"""客户端侧「交易连接失败」根因诊断（自 `env.py` 按职责拆出）。

本模块只做**只读**的客户端日志分析：定位日志文件 → 读「模块授权」串 →
判定是否被客户端的「严格连接校验」拒绝。不 import xtquant、不连接券商，
因此可安全用于任何诊断路径。

★ 为什么从 `env.py` 拆出（2026-09-28）：`scripts/check_execution_architecture.py`
的 Gate 4 要求 `backend` 下**非测试** `*.py` 不得超过 50KB，而 `env.py` 因本轮新增
诊断逻辑达到 54.0KB。客户端日志定位 / 授权串读取 / 根因判定是一块**独立且内聚**的
职责，故整体搬出。搬迁为**逐行原样**（仅调整 import），并由 `env.py` 再导出全部符号，
保证既有导入路径（如 `adapter.py` 的 `from .env import _latest_login_log`）与壳模块
`xtquant_client.xtp` 的 monkeypatch 兼容层（`_common._shell_attr`）都不受影响。
"""

import os
import re

from ._common import _candidate_roots, _normalize


# 客户端主日志文件名前缀（按客户端模式区分）：
#   完整版 XtItClient → XtClient_YYYYMMDD.log（交易主日志）
#   极速版 XtMiniQmt → XtMiniQmt_YYYYMMDD.log（交易/委托主日志）
#   行情子进程 miniquote → XtMiniQuote_YYYYMMDD.log（行情主日志，仅兜底）
# ★ 2026-09-28 修正：旧实现只认 "XtClient_" 前缀 ⇒ 极速版（MiniQMT）永远取不到
#   日志（恒返回 ''），rc=-1 的真实归因证据被整段丢弃。
_CLIENT_LOG_RE = re.compile(r"^(?:XtClient|XtMiniQmt|XtMiniQuote)_\d{8}.*\.log$")
_CLIENT_LOG_MAIN_RE = re.compile(r"^(?:XtClient|XtMiniQmt|XtMiniQuote)_\d{8}\.log$")
# 行情子进程日志（miniquote 单独进程）：**不含交易通道证据**，只能作最后兜底。
# ★ 2026-09-28 修正：XtMiniQuote 由行情线程高频写入，mtime 永远最新。若只按 mtime
#   选日志，会稳定选中行情日志 → 「pid not allowed」与授权串都读不到，诊断恒为空。
#   因此改为「交易主日志 > 交易辅助日志 > 行情日志」的确定性优先级。
_QUOTE_LOG_RE = re.compile(r"^XtMiniQuote_")
_TRADE_LOG_MAIN_RE = re.compile(r"^(?:XtClient|XtMiniQmt)_\d{8}\.log$")


def _log_rank(fname: str) -> int:
    """日志优先级：2=交易主日志 / 1=交易辅助日志 / 0=行情日志（兜底）。"""
    if _QUOTE_LOG_RE.match(fname):
        return 0
    return 2 if _TRADE_LOG_MAIN_RE.match(fname) else 1


def _latest_login_log(trade_dir: str) -> str:
    """从客户端交易日志中提取最近一次“登录成功”记录（best-effort，兼容两种客户端）。"""
    try:
        log_dir = os.path.join(trade_dir, "log")
        if not os.path.isdir(log_dir):
            return ""
        logs = [f for f in os.listdir(log_dir) if _CLIENT_LOG_RE.match(f)]
        if not logs:
            return ""
        # 优先取“主日志”(<前缀>_YYYYMMDD.log，纯日期、无附加后缀)，
        # 避免选中 FormulaOutput / Debug / PerformanceFile / Message 等辅助日志。
        _pool = [f for f in logs if _CLIENT_LOG_MAIN_RE.match(f)] or logs
        newest = max(_pool,
                     key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
        data = open(os.path.join(log_dir, newest), "rb").read()
        txt = data.decode("gb18030", errors="ignore")
        # 交易登录成功在主日志里的标记词
        hits = [ln.strip() for ln in txt.splitlines()
                if ("LoginSuccess" in ln or "登录成功" in ln)][-1:]
        return f"{newest}: {hits[0][-90:] if hits else '未找到登录成功记录'}"
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 客户端侧「交易连接失败」根因诊断 ----------------
# 背景（2026-09-28 真机实测）：QMT 客户端小版本升级后，券商服务端下发的「模块授权」
# 串（客户端日志 `receive module auth string : {...}`）发生变化：
#     mdl_auth_xttrader_strict_connection_check : 0 -> 1
#     mdl_auth_xtdata_strict_connection_check   : 0 -> 1
#     mdl_auth_xtquant_no_pid_check             : 0（保持 0 = 启用 PID 校验）
# 后果：客户端对调用 xtquant 的**外部进程**做 PID 白名单校验，未授权进程被直接拒绝：
#     客户端日志 `quant session N, pid X not allowed, return`
#               `[quant]XtQuantServer:: connect ret error-1`
#     平台侧     XtQuantTrader.connect() 恒返回 -1（**行情侧 xtdata 不受影响**）
# 该开关由券商服务端下发，平台无法自行改写 —— 只能精确告知用户去申请授权，
# 而不是把用户误导到「重装 SDK / 检查行情登录 / 会话冲突」这些无关方向上。
_AUTH_STRICT_KEYS = (
    "mdl_auth_xttrader_strict_connection_check",
    "mdl_auth_xtdata_strict_connection_check",
    "mdl_auth_xtquant_strict_connection_check",
)
_AUTH_NO_PID_KEY = "mdl_auth_xtquant_no_pid_check"
_AUTH_SNIPPET_RE = re.compile(
    r'"(mdl_auth_[A-Za-z0-9_]+)"\s*:\s*(\d+)')


def _client_log_dirs(trade_dir: str) -> list[str]:
    """客户端日志目录候选（交易数据目录/log + 客户端根/log，去重保序、只留存在者）。"""
    dirs: list[str] = []
    seen: set[str] = set()

    def _add(d: str) -> None:
        if d and d not in seen:
            seen.add(d)
            dirs.append(d)

    try:
        roots = _candidate_roots(trade_dir)
    except Exception:  # noqa: BLE001
        roots = []
    for r in (roots or [trade_dir]):
        _add(os.path.join(r, "log"))
    if trade_dir:
        _add(os.path.join(trade_dir, "log"))
    return [d for d in dirs if os.path.isdir(d)]


def _newest_client_log(trade_dir: str) -> str:
    """返回客户端最新的**交易**日志完整路径（找不到返回 ''）。

    排序键 = (日志优先级, mtime)：交易主日志永远胜过行情日志，同级再比 mtime。
    这样结果确定且与「是否有交易证据」一致（见 _log_rank 注释）。
    """
    best, best_key = "", (-1, -1.0)
    for d in _client_log_dirs(trade_dir):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            if not _CLIENT_LOG_RE.match(f):
                continue
            p = os.path.join(d, f)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            key = (_log_rank(f), m)
            if key > best_key:
                best, best_key = p, key
    return best


def _tail_text(path: str, max_bytes: int = 262144) -> str:
    """读取文件尾部 max_bytes 文本（utf-8 → gb18030 容错，失败返回 ''）。"""
    return _read_window(path, tail=True, max_bytes=max_bytes)


def _head_text(path: str, max_bytes: int = 524288) -> str:
    """读取文件头部 max_bytes 文本（utf-8 → gb18030 容错，失败返回 ''）。

    为什么需要头部：客户端的「模块授权串」是**登录/启动时一次性下发**并写在日志开头
    （实测 2026-09-28 的 XtMiniQmt 日志：授权串在偏移 7885，而当日日志已涨到 354KB），
    只读尾部必然漏掉它 —— 那样严格连接校验的判定就永远不会被触发。
    """
    return _read_window(path, tail=False, max_bytes=max_bytes)


def _read_window(path: str, tail: bool, max_bytes: int) -> str:
    """读取文件头/尾窗口内的文本（utf-8 → gb18030 容错解码，失败返回 ''）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if tail and size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read(max_bytes)
    except OSError:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="ignore")


def read_client_auth_flags(trade_dir: str) -> dict:
    """读回券商下发给客户端的最新「模块授权」开关（读日志，只读无副作用）。

    授权串写在日志**头部**（登录时下发），故先读头再读尾兜底；找不到时
    found=False（不臆测，交由上层走通用排查文案）。
    返回 {"found": bool, "flags": {键: 值}, "strict_xttrader": int|None,
          "strict_xtdata": int|None, "no_pid_check": int|None, "log": str}。
    """
    out: dict = {"found": False, "flags": {}, "strict_xttrader": None,
                 "strict_xtdata": None, "no_pid_check": None, "log": ""}
    p = _newest_client_log(trade_dir)
    if not p:
        return out
    out["log"] = p
    seg = ""
    for txt in (_head_text(p), _tail_text(p)):
        if not txt:
            continue
        idx = txt.rfind("receive module auth string")
        if idx >= 0:
            seg = txt[idx:idx + 300000]
            break
    if not seg:
        return out
    flags = {k: int(v) for k, v in _AUTH_SNIPPET_RE.findall(seg)}
    if not flags:
        return out
    out["found"] = True
    out["flags"] = flags
    out["strict_xttrader"] = flags.get("mdl_auth_xttrader_strict_connection_check")
    out["strict_xtdata"] = flags.get("mdl_auth_xtdata_strict_connection_check")
    out["no_pid_check"] = flags.get(_AUTH_NO_PID_KEY)
    return out


def diagnose_trade_connect(trade_dir: str) -> dict:
    """诊断「交易连接 rc=-1」的客户端侧真实原因（只读客户端日志，无副作用）。

    返回 {"reason","title","evidence","steps","auth","log"}：
      reason == "pid_not_allowed" → 客户端启用严格连接校验、拒绝未授权进程（最优先透出）
      reason == "strict_check"    → 授权串显示已开启严格校验，但本轮日志未见显式拒绝
      reason == ""                → 无明确证据（上层保持原有四步排查文案，不臆测）

    ★ 回退策略：严格校验是**账号级授权**（券商服务端按资金账号下发），与该账号跑到
    哪个客户端模式（完整版 userdata / 极速版 userdata_mini）无关。因此当 primary
    数据目录的日志无证据时，再查同根的**另一数据目录**日志一次 —— 否则用户用
    「完整版」模式连接时会拿不到任何诊断（实测：授权串只出现在当时在跑的极速版日志里）。
    """
    res = _diagnose_one(trade_dir)
    if res.get("reason"):
        return res
    try:
        roots = _candidate_roots(trade_dir)
    except Exception:  # noqa: BLE001
        roots = []
    base = roots[0] if roots else ""
    if base:
        for sib in ("userdata", "userdata_mini"):
            alt = os.path.join(base, sib)
            if os.path.isdir(alt) and _normalize(alt) != _normalize(trade_dir):
                alt_res = _diagnose_one(alt)
                if alt_res.get("reason"):
                    alt_res["title"] += f"（证据取自同客户端另一数据目录：{alt}）"
                    alt_res["sibling_of"] = trade_dir
                    return alt_res
    return res


def _diagnose_one(trade_dir: str) -> dict:
    """单目录诊断（diagnose_trade_connect 的实现体，不做同根回退）。"""
    res: dict = {"reason": "", "title": "", "evidence": [], "steps": "",
                 "auth": {}, "log": ""}
    auth = read_client_auth_flags(trade_dir)
    res["auth"] = auth
    res["log"] = auth.get("log") or ""
    p = auth.get("log") or _newest_client_log(trade_dir)
    if not p:
        return res
    res["log"] = p
    txt = _tail_text(p)
    if not txt:
        return res
    lines = txt.splitlines()[-1200:]
    not_allowed = [ln.strip() for ln in lines if "not allowed" in ln]
    connect_err = [ln.strip() for ln in lines if "connect ret error" in ln]
    if not_allowed:
        res["reason"] = "pid_not_allowed"
        res["title"] = (
            "客户端启用了「量化连接严格校验」，拒绝了本平台进程的 xtquant 连接"
            f"（客户端日志：{not_allowed[-1][-140:]}）")
        res["evidence"] = (not_allowed[-2:] + connect_err[-1:])[:3]
    elif (auth.get("strict_xttrader") == 1
          and auth.get("no_pid_check") in (0, None)):
        res["reason"] = "strict_check"
        res["title"] = (
            "客户端授权串显示已开启「xttrader 严格连接校验」"
            f"（{_AUTH_STRICT_KEYS[0]}=1，{_AUTH_NO_PID_KEY}={auth.get('no_pid_check')}）")
        res["evidence"] = connect_err[-2:]
    if not res["reason"]:
        return res
    res["steps"] = (
        "该开关由券商服务端下发的模块授权控制（xttrader/xtdata "
        "strict_connection_check=1、xtquant_no_pid_check=0），平台侧无法自行改写。\n"
        "  1) 联系券商，为该资金账号申请「程序化交易/外部策略接入」授权，"
        "并要求关闭「xtquant 严格连接校验」（或把调用进程加入 PID 白名单）；\n"
        "  2) 若券商要求白名单：将本平台后端进程（打包态 qmt_work.exe / 开发态 "
        "python.exe）的完整路径加入客户端「设置 → 量化接口/白名单」，"
        "或在客户端内以「极简模式」登录后再连；\n"
        "  3) 若客户端刚升级，请确认券商侧授权已随新版本重新下发"
        "（客户端重登一次即可拉取最新授权串）；\n"
        "  4) 可先在客户端内运行一次内置 Python 策略，确认策略通道本身可用；\n"
        "  5) 【不等券商授权的替代通道】在客户端内运行 qmt_work 的大 QMT 桥接策略"
        "（agent_bigqmt/BIGQMT_AGENT.py，文件桥零部署），随后在「券商接入」新建连接"
        "时接入模式选「大 QMT 桥接·文件」（connector_key=qmt.big.bridge.file）—— "
        "该通道走客户端内置 Python，**不受 PID 白名单限制**。\n"
        "  注意：本问题**只影响交易通道**，行情（xtdata）不受影响，行情功能可继续使用。")
    return res


__all__ = [
    '_CLIENT_LOG_RE',
    '_CLIENT_LOG_MAIN_RE',
    '_QUOTE_LOG_RE',
    '_TRADE_LOG_MAIN_RE',
    '_log_rank',
    '_latest_login_log',
    '_AUTH_STRICT_KEYS',
    '_AUTH_NO_PID_KEY',
    '_AUTH_SNIPPET_RE',
    '_client_log_dirs',
    '_newest_client_log',
    '_tail_text',
    '_head_text',
    '_read_window',
    'read_client_auth_flags',
    'diagnose_trade_connect',
    '_diagnose_one',
]
