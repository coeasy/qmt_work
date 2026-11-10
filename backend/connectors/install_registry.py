"""多 QMT 安装的发现与端口台账（多券商并行支持的底座）。

为什么需要它
------------
实测（2026-11-08）：本机同时装了 4 套 QMT，其中两套**同时运行**，而它们的
``config/broker.ini`` 逐字节相同 —— 于是两个 ``XtItClient.exe`` 同时
``LISTENING 0.0.0.0:58600``。Windows 在 ``SO_REUSEADDR`` 下允许这种重复绑定，
**连接落到哪一家是不确定的**。对只读行情是"数据可能来自另一家"，对任何写操作
就是**串户**。

本模块只做三件事，且都**只依赖文件系统事实**，不猜：

1. ``discover()``   —— 扫出所有安装，给出指纹（exe 时间、xtquant 代次、是否有极简模式组件）；
2. ``conflicts()``  —— 找出**声明端口撞车**的安装对（不是"猜会不会撞"，是比对配置里的字面值）；
3. ``plan_ports()`` —— 给多券商并行生成一份**互不冲突**的端口方案。

设计纪律
--------
* **不硬编码券商名**：安装靠 ``bin.x64/XtItClient.exe`` 存在性识别，标签由目录名派生，
  也可显式覆盖。光大/国信/中金都只是"某个目录"，代码里不出现它们的名字。
* **不静默择一**：``conflicts()`` 返回冲突对，由调用方决定报错还是改端口；
  本模块**不**提供"自动挑一个用"的接口 —— 那正是串户的来源。
* **活体监听探测可注入**：``listening_ports`` 参数让测试无需真的开端口；
  生产传 ``_win_listening_ports()``。
"""
from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

#: 识别一套安装的标志文件（相对安装根）。
_CLIENT_EXE = os.path.join("bin.x64", "XtItClient.exe")
_MINI_EXE = os.path.join("bin.x64", "XtMiniQmt.exe")
_MINIBROKER_EXE = os.path.join("bin.x64", "minibroker.exe")
_XTDATA_DIR = os.path.join("bin.x64", "Lib", "site-packages", "xtquant")

#: broker.ini 里所有**端口**语义的键（其余键不是端口，不要当端口用）。
_PORT_KEYS = ("port", "pbserverport", "serverport")

#: 端口方案的默认步长：同机并行时，第 i 套安装整体偏移 i*STRIDE。
STRIDE = 100


class InstallError(RuntimeError):
    """安装布局不符合预期（缺关键文件/配置不可解析）。"""


@dataclass(frozen=True)
class Install:
    """一套 QMT 安装的只读指纹。"""

    label: str
    root: str
    #: ``XtItClient.exe`` 的 (size, mtime) —— 用于判断客户端代次。
    exe_size: int
    exe_mtime: float
    #: xtquant 包代次：以 ``xtpythonclient`` pyd 的 mtime 年份 + 是否有 xtdatacenter 判定。
    xtquant_year: int
    has_xtdatacenter: bool
    has_mini: bool
    has_minibroker: bool
    #: ``broker.ini`` 声明的端口，形如 ``{"formulaworker.serverport": 58600}``。
    declared_ports: Mapping[str, int] = field(default_factory=dict)
    #: ``xtdata.ini`` 里的 ``address``（可能为空）。
    xtdata_addr: str = ""

    @property
    def userdata(self) -> str:
        return os.path.join(self.root, "userdata")

    @property
    def userdata_mini(self) -> str:
        return os.path.join(self.root, "userdata_mini")

    @property
    def supports_mini(self) -> bool:
        """极简模式组件是否齐全（``XtMiniQmt.exe`` + ``minibroker.exe``）。

        实测差异：光大 两者都有；国信只有 ``miniquote.exe``，缺 ``XtMiniQmt.exe``
        与 ``minibroker.exe`` ⇒ 那套**跑不了极简模式**，也就没有 xttrader 直连的形态。
        """
        return self.has_mini and self.has_minibroker

    @property
    def generation(self) -> str:
        """粗代次标签，仅用于报告与日志，**不参与任何能力判定**。"""
        if self.has_xtdatacenter and self.xtquant_year >= 2024:
            return "G2025"
        if self.xtquant_year >= 2023:
            return "G2023"
        return "G2022"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "root": self.root,
            "generation": self.generation,
            "xtquant_year": self.xtquant_year,
            "has_xtdatacenter": self.has_xtdatacenter,
            "supports_mini": self.supports_mini,
            "declared_ports": dict(self.declared_ports),
            "xtdata_addr": self.xtdata_addr,
        }


@dataclass(frozen=True)
class Conflict:
    """两套安装的端口撞车。"""

    port: int
    left: str
    right: str
    keys: tuple[str, ...]

    def describe(self) -> str:
        return (f"端口 {self.port} 被 {self.left} 与 {self.right} 同时声明"
                f"（{', '.join(self.keys)}）")


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def _read_ini(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    # 保留键名大小写：``pbserverport`` 与 ``pbServerPort`` 是同一个键，
    # 但 ``port`` 与 ``serverport`` 是两个不同的键，不能混。
    cp.optionxform = str  # type: ignore[assignment]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            cp.read_file(f)
    except FileNotFoundError as exc:
        raise InstallError(f"配置不存在: {path}") from exc
    return cp


def read_declared_ports(broker_ini: str) -> dict[str, int]:
    """从 ``broker.ini`` 抽出 ``{section.key: port}``。

    只认 ``port`` / ``pbserverport`` / ``serverport`` 三个键；值必须是正整数，
    否则**跳过并保留原文**（由调用方决定是否报错）—— 静默把非法值当 0 会让
    "端口没配"看起来像"端口配了但没起"，是两类完全不同的故障。
    """
    cp = _read_ini(broker_ini)
    out: dict[str, int] = {}
    for section in cp.sections():
        for key, raw in cp.items(section):
            if key.lower() not in _PORT_KEYS:
                continue
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if value <= 0 or value > 65535:
                continue
            out[f"{section}.{key}"] = value
    return out


def read_xtdata_addr(xtdata_ini: str) -> str:
    """读 ``xtdata.ini`` 的 ``[client_xtdata] address``；不存在则返回空串。"""
    if not os.path.exists(xtdata_ini):
        return ""
    cp = _read_ini(xtdata_ini)
    for section in cp.sections():
        if section.lower() != "client_xtdata":
            continue
        for key, raw in cp.items(section):
            if key.lower() == "address":
                return str(raw).strip()
    return ""


def _xtquant_fingerprint(xtquant_dir: str) -> tuple[int, bool]:
    """返回 (pyd 最新 mtime 的年份, 是否有 xtdatacenter)。"""
    year = 0
    if os.path.isdir(xtquant_dir):
        best = 0.0
        for name in os.listdir(xtquant_dir):
            if not name.startswith("xtpythonclient") or not name.endswith(".pyd"):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(xtquant_dir, name))
            except OSError:
                continue
            best = max(best, mtime)
        if best:
            import datetime as _dt
            year = _dt.datetime.fromtimestamp(best).year
    has_dc = os.path.exists(os.path.join(xtquant_dir, "xtdatacenter.py"))
    return year, has_dc


def _label_from_root(root: str) -> str:
    return os.path.basename(os.path.normpath(root)) or root


def find_install_root(start: str, *, max_up: int = 4) -> str | None:
    """从任意客户端路径向上定位**安装根**（含 ``bin.x64/XtItClient.exe`` 的那层）。

    为什么需要它：上层拿到的路径形态不统一 —— 可能是安装根、也可能是
    ``userdata`` / ``userdata_mini`` / ``bin.x64`` / ``bin.x64/..``。
    写死"盘符 + 目录名"既不可移植也不通用，正确做法是**从已知路径回溯**。

    返回 ``None`` 表示向上 ``max_up`` 层都没找到（不是错误，只是这个路径不属于安装）。
    """
    cur = os.path.abspath(str(start or ""))
    if not cur:
        return None
    for _ in range(max_up + 1):
        if os.path.exists(os.path.join(cur, _CLIENT_EXE)):
            return str(Path(cur))
        parent = os.path.dirname(cur)
        if parent == cur:          # 到盘根，停
            return None
        cur = parent
    return None


def load_install(root: str, *, label: str | None = None) -> Install:
    """按路径加载一套安装；缺 ``XtItClient.exe`` 直接抛 ``InstallError``。"""
    exe = os.path.join(root, _CLIENT_EXE)
    if not os.path.exists(exe):
        raise InstallError(f"不是 QMT 安装目录（缺 {_CLIENT_EXE}）: {root}")
    try:
        st = os.stat(exe)
    except OSError as exc:
        raise InstallError(f"无法读取 {exe}: {exc}") from exc

    xt_dir = os.path.join(root, _XTDATA_DIR)
    year, has_dc = _xtquant_fingerprint(xt_dir)

    broker_ini = os.path.join(root, "config", "broker.ini")
    declared = read_declared_ports(broker_ini) if os.path.exists(broker_ini) else {}

    xtdata_ini = os.path.join(xt_dir, "xtdata.ini")

    return Install(
        label=label or _label_from_root(root),
        root=str(Path(root)),
        exe_size=st.st_size,
        exe_mtime=st.st_mtime,
        xtquant_year=year,
        has_xtdatacenter=has_dc,
        has_mini=os.path.exists(os.path.join(root, _MINI_EXE)),
        has_minibroker=os.path.exists(os.path.join(root, _MINIBROKER_EXE)),
        declared_ports=declared,
        xtdata_addr=read_xtdata_addr(xtdata_ini),
    )


def discover(roots: Iterable[str], *, labels: Mapping[str, str] | None = None) -> list[Install]:
    """扫描若干**父目录**，返回其中所有 QMT 安装。

    ``roots`` 是父目录列表（例如 ``["P:/stock"]``）；非安装目录被安静跳过 ——
    "这个目录不是 QMT" 是正常情况，不是错误。只有**显式指定**的路径缺关键文件时
    才应报错（用 ``load_install`` 直接加载即可）。
    """
    labels = labels or {}
    found: list[Install] = []
    seen: set[str] = set()
    for parent in roots:
        if not os.path.isdir(parent):
            continue
        for name in sorted(os.listdir(parent)):
            root = os.path.join(parent, name)
            if not os.path.isdir(root):
                continue
            if not os.path.exists(os.path.join(root, _CLIENT_EXE)):
                continue
            key = os.path.normcase(os.path.abspath(root))
            if key in seen:
                continue
            seen.add(key)
            try:
                found.append(load_install(root, label=labels.get(name)))
            except InstallError:
                continue
    return found


# ----------------------------------------------------------------------
# 冲突与端口方案
# ----------------------------------------------------------------------
def conflicts(installs: Iterable[Install]) -> list[Conflict]:
    """找出**声明端口**撞车的安装对。

    只比对 ``broker.ini`` 里的字面值：两份配置写了同一个端口号，就意味着
    同时运行必然重复绑定。**不做启发式推断**。
    """
    items = list(installs)
    out: list[Conflict] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            by_port: dict[int, list[str]] = {}
            for key, port in a.declared_ports.items():
                if b.declared_ports.get(key) == port:
                    by_port.setdefault(port, []).append(key)
            for port, keys in sorted(by_port.items()):
                out.append(Conflict(port=port, left=a.label, right=b.label,
                                    keys=tuple(sorted(keys))))
    return out


def plan_ports(installs: Iterable[Install], *, stride: int = STRIDE) -> dict[str, dict[str, int]]:
    """给多券商并行生成互不冲突的端口方案。

    规则（确定性、可复现）：
      * 第 i 套安装的每个声明端口都加上 ``i * stride``（第 0 套保持原样）；
      * ``xtdata.ini`` 的 address 端口同样偏移 —— 它必须与 broker 侧**成对改**，
        否则行情与交易会指向不同实例；
      * 结果超过 65535 直接抛错 —— **不截断、不回绕**（回绕会撞上别的端口）；
      * 方案内部自撞也抛错（stride 太小会撞），绝不返回一份"看着能跑"的表。

    返回 ``{label: {原键: 新端口, "__xtdata_port__": 新端口}}``。
    """
    if stride <= 0:
        raise InstallError(f"stride 必须为正整数，收到 {stride}")
    items = list(installs)
    # ★ 方案按 label 做键。两套安装目录名相同（例如都叫 qmt）时，后一套会
    #   **静默覆盖**前一套 —— count=4 却只给出 3 套的方案，而且没人报错。
    #   多券商并行最怕这种"少给了一家的配置"，直接判错比悄悄丢一条强。
    labels: dict[str, str] = {}
    for inst in items:
        first = labels.get(inst.label)
        if first is not None:
            raise InstallError(
                f"两套安装同名 {inst.label!r}（{first} 与 {inst.root}）："
                f"端口方案以 label 为键会互相覆盖，请用显式 label 区分后再生成")
        labels[inst.label] = inst.root

    plan: dict[str, dict[str, int]] = {}
    used: dict[int, str] = {}

    for idx, inst in enumerate(items):
        shift = idx * stride
        row: dict[str, int] = {}
        for key, port in inst.declared_ports.items():
            new_port = port + shift
            if new_port > 65535:
                raise InstallError(
                    f"{inst.label} 的 {key} 偏移后 {new_port} 超出端口上限；请减小 stride")
            row[key] = new_port

        xt_port = _port_of_addr(inst.xtdata_addr)
        if xt_port:
            new_xt = xt_port + shift
            if new_xt > 65535:
                raise InstallError(
                    f"{inst.label} 的 xtdata 端口偏移后 {new_xt} 超限；请减小 stride")
            row["__xtdata_port__"] = new_xt

        for key, port in row.items():
            owner = used.get(port)
            if owner is not None:
                raise InstallError(
                    f"端口方案自撞：{inst.label}.{key}={port} 已被 {owner} 占用；请加大 stride")
            used[port] = f"{inst.label}.{key}"
        plan[inst.label] = row

    return plan


_ADDR_RE = re.compile(r":(\d{1,5})\s*$")


def _port_of_addr(addr: str) -> int:
    if not addr:
        return 0
    m = _ADDR_RE.search(addr)
    if not m:
        return 0
    try:
        port = int(m.group(1))
    except ValueError:
        return 0
    return port if 0 < port <= 65535 else 0


# ----------------------------------------------------------------------
# 活体监听（可注入，便于测试）
# ----------------------------------------------------------------------
def parse_netstat_listeners(text: str) -> dict[int, list[str]]:
    """从 ``netstat -ano`` 文本里解析出 ``{port: [pid, ...]}``（仅 LISTENING）。

    刻意**只解析 LISTENING**：``ESTABLISHED`` 的本地端口是临时端口，
    把它算成"监听"会让冲突检测满屏假红。
    """
    out: dict[int, list[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        if parts[-2].upper() != "LISTENING":
            continue
        addr = parts[1]
        m = _ADDR_RE.search(addr)
        if not m:
            continue
        try:
            port = int(m.group(1))
        except ValueError:
            continue
        pid = parts[-1]
        out.setdefault(port, []).append(pid)
    return out


def double_bound(listeners: Mapping[int, list[str]]) -> dict[int, list[str]]:
    """同一端口被**多个 PID** 监听 —— 就是重复绑定（串户风险）。"""
    return {port: pids for port, pids in listeners.items() if len(set(pids)) > 1}


ListenerProbe = Callable[[], "Mapping[int, list[str]] | None"]


def audit(installs: Iterable[Install], probe: ListenerProbe | None = None) -> dict:
    """汇总一份可直接进 ``health`` 的审计结果。

    ``probe`` 为 ``None`` 时**只做静态比对**（声明端口撞车），不读活体状态 ——
    这样单元测试无需真的开端口，也不会因为本机没跑 QMT 就判红。

    ★ 三态纪律：``live_probe`` 区分
      * ``"skipped"``     —— 没传 probe，**没检查过**；
      * ``"unavailable"`` —— 传了但探针拿不到结果（如非 Windows / netstat 失败），
        **仍然没检查过**；
      * ``"ok"``          —— 真的比对过。
    前两种都**不能**当作"检查了且无冲突"——那正是本仓库最忌的静默假绿。

    ★★ ``ok`` 的含义必须是"**要求过的检查都做成了且没问题**"。
    上一版只算 ``not conflicts and not double_bound``：用户明确要了活体检查、
    而 netstat 挂掉时 ``double_bound`` 是空集，于是 ``ok=True`` ——
    绿灯读起来是"查过了，没有重复绑定"，事实是"根本没查"。三态字段虽然加了，
    ``ok`` 却没跟着收紧，等于假绿换了个地方藏着。现改为：
      * ``unavailable`` ⇒ ``ok=False``（要求过的检查没做成，不能判绿）；
      * ``skipped``     ⇒ 允许 ``ok=True``，但 ``ok_basis="static_only"``
        并且 ``note`` 明说"仅静态比对，未查活体"；
      * ``ok``          ⇒ ``ok_basis="static_plus_live"``。
    """
    items = list(installs)
    report: dict = {
        "installs": [i.to_dict() for i in items],
        "declared_conflicts": [{"port": c.port, "left": c.left, "right": c.right,
                                "keys": list(c.keys), "message": c.describe()}
                               for c in conflicts(items)],
        "double_bound": {},
        "live_probe": "skipped",
        "live_checked": False,
        "ok": True,
    }
    if probe is not None:
        listeners = probe()
        if listeners is None:
            report["live_probe"] = "unavailable"
        else:
            report["live_probe"] = "ok"
            report["live_checked"] = True
            db = double_bound(listeners)
            report["double_bound"] = {str(p): sorted(set(pids)) for p, pids in sorted(db.items())}
    report["ok_basis"] = ("static_plus_live" if report["live_probe"] == "ok"
                          else "static_only" if report["live_probe"] == "skipped"
                          else "live_unavailable")
    report["ok"] = (not report["declared_conflicts"]
                    and not report["double_bound"]
                    and report["live_probe"] != "unavailable")
    if report["live_probe"] == "skipped":
        report["note"] = ("仅静态比对 broker.ini 声明端口，未查活体监听；"
                          "需要活体结论请带 live=1。")
    elif report["live_probe"] == "unavailable":
        report["note"] = ("活体探针不可用（netstat 缺失/失败/非 Windows）："
                          "本轮**没有**做过重复绑定检查，不能据此判定无串户风险。")
    return report


def decode_console_bytes(raw: bytes) -> str:
    """把 Windows 控制台命令的原始输出解成文本。

    ★ 必须按**多编码**尝试，不能假定 UTF-8：``netstat -ano`` 在中文 Windows 上
      输出 OEM 代码页（GBK/cp936）。若用 ``text=True``（UTF-8 解码），会抛
      ``UnicodeDecodeError`` 并让 ``stdout`` 变成空串 —— 于是"本机明明两个进程
      在抢 58600"，审计却报"无冲突"。这是典型的**静默假绿**，比报错更坏。
    """
    for enc in ("utf-8", "cp936", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _win_listening_ports() -> dict[int, list[str]] | None:
    """生产用探针：跑一次 ``netstat -ano``。

    用 ``subprocess`` 而非 psutil —— 后端运行时不保证装了 psutil，
    而 ``netstat`` 是 Windows 自带。

    ★ 拿不到结果时返回 ``None``，**不是空字典**。空字典会被
      ``double_bound()`` 读成"没有任何重复绑定" ⇒ 审计报"无冲突"。
      在非 Windows / netstat 被裁掉 / 命令超时的情况下，这是**静默假绿**：
      用户看到的绿灯含义是"检查过了，没事"，而事实是"根本没检查"。
    """
    import subprocess
    try:
        cp = subprocess.run(["netstat", "-ano"], capture_output=True,
                            timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    raw = cp.stdout or b""
    if not raw.strip():
        return None
    return parse_netstat_listeners(decode_console_bytes(raw))


__all__ = [
    "Install", "Conflict", "InstallError", "STRIDE",
    "load_install", "discover", "find_install_root",
    "read_declared_ports", "read_xtdata_addr",
    "conflicts", "plan_ports", "parse_netstat_listeners", "double_bound",
    "decode_console_bytes", "audit", "_win_listening_ports",
]
