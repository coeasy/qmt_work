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

from core.errors import swallow

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
#
# ★ 2026-10-09（R22）补充第二种、更常见的根因（光大 QMT 真机实测）：
#     授权串里 `mdl_auth_xtquant=0` / `mdl_auth_gt_ipc_pair=0`、三个 strict_* **全为 0**
#     （即**并非** PID 白名单场景），客户端日志出现：
#          `CIPCManager::init, not auth:mdl_auth_gt_ipc_pair`
#          `The XtQuantServer is not allowed to start.`
#     ⇒ 客户端**根本没有启动量化服务进程**，任何外部 connect() 必然失败。
#     与 PID 拒绝的区别是决定性的：一个要「加白名单」，一个要「申请 xtquant 模块授权」。
#     旧实现用 `"not allowed" in ln` 粗判，把后者误报成前者 —— 属「假告状」家族。
_AUTH_STRICT_KEYS = (
    "mdl_auth_xttrader_strict_connection_check",
    "mdl_auth_xtdata_strict_connection_check",
    "mdl_auth_xtquant_strict_connection_check",
)
_AUTH_NO_PID_KEY = "mdl_auth_xtquant_no_pid_check"
#: xtquant 模块总开关与量化 IPC 配对授权：为 0 时客户端**根本不启动** XtQuantServer
#: （实测日志 `CIPCManager::init, not auth:mdl_auth_gt_ipc_pair` +
#:  `The XtQuantServer is not allowed to start.`），此时任何外部进程 connect() 必 -1，
#: 与「严格连接校验 / PID 白名单」是**两回事**，必须分开归因。
_AUTH_XTQUANT_KEY = "mdl_auth_xtquant"
_AUTH_IPC_PAIR_KEY = "mdl_auth_gt_ipc_pair"
#: 授权串标记（日志原文：`[auth log] [CProxyClient] receive module auth string : { ... }`）
_AUTH_MARKER = "receive module auth string"
#: 客户端侧「量化服务未获授权启动」标记（与 PID 拒绝是不同根因）。
_SERVER_BLOCKED_MARKER = "The XtQuantServer is not allowed to start."
#: 授权串 JSON 是按单行写下的（实测 49,910 B），从偏移读 400KB 足够覆盖；
#: 若未来客户端改成跨行，偏移读取仍能拿到完整 JSON。
_AUTH_SEG_BYTES = 400_000
#: 真正的「PID 白名单拒绝」行形如 `quant session N, pid X not allowed, return`。
#: ★ 旧实现用 `"not allowed" in ln` 粗判，会把 `The XtQuantServer is not allowed to start.`
#:   也归为 PID 拒绝 ⇒ **归因错误**（把「服务没授权启动」说成「你的进程被拉黑」，
#:   用户按 PID 白名单方向排查必然徒劳）。改为匹配 pid + not allowed 的组合。
_PID_DENY_RE = re.compile(r"\bpid\b[^\n]{0,40}not allowed", re.IGNORECASE)
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
    return _decode_bytes(raw)


def _decode_bytes(raw: bytes) -> str:
    """utf-8 → gb18030 容错解码（两者都失败时用 gb18030 忽略坏字节，绝不抛错）。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="ignore")


# ---------------- 全文件「标记扫描」 ----------------
# ★ 2026-10-09（R22）：固定窗口（头 512KB / 尾 256KB）在长跑日志上**必然漏读**。
#   实测当日光大 QMT 主日志 XtClient_20261009.log = 17,196,674 B / 103,592 行：
#     · 授权串 `receive module auth string` 在**第 9719 行 / 偏移 1,573,933 B**
#     · 拒绝行 `The XtQuantServer is not allowed to start.` 在第 10703/17483/93554 行
#       （偏移 1,810,790 / 3,049,117 / 15,543,174 B）
#   而「头 512KB」只覆盖前 3242 行（授权串/拒绝行全部落在窗口之外），「尾 256KB」从
#   16.9MB 起（同样漏掉）。后果：**同一台机器早上诊断正确、晚上静默失效** —— 诊断
#   结论随当天日志体量漂移，是「假绿灯」家族里最难查的一种（不是判错，而是不判）。
#   改为按块前扫全文件、命中即收行；上限 _SCAN_MAX_BYTES 防止病态超大日志把连接失败
#   路径拖死（超限时如实返回已扫到的部分，不臆测）。
_SCAN_CHUNK = 1 << 20          # 每次读取 1 MiB
_SCAN_MAX_BYTES = 64 << 20     # 扫描上限 64 MiB
_SCAN_KEEP_LINES = 8           # 每个标记只保留最后 8 条（够给证据，不涨内存）


def _scan_marker_lines(path: str, markers, max_bytes: int = _SCAN_MAX_BYTES) -> dict:
    """按块扫描全文件，返回 ``{marker: [(绝对字节偏移, 行文本), ...]}``。

    - 分块读取并保留跨块残行，避免把一行切成两半导致漏匹配；
    - 记录**绝对偏移**，调用方据此再按窗口读取该标记之后的原文
      （授权串 JSON 实测是单行 49,910 B，但不假定它永远单行 —— 有偏移就不怕换行）；
    - 命中行用 `_decode_bytes` 容错解码（同一日志里可能混有 utf-8 与 gb18030 字节）；
    - 每个标记只保留最后 `_SCAN_KEEP_LINES` 条；任何 OSError 都退回已扫到的部分，
      绝不因读日志失败而让诊断抛错（诊断自身崩溃比诊断不到更糟）。
    """
    out: dict = {m: [] for m in markers}
    pairs = [(m, m.encode("utf-8")) for m in markers]
    try:
        size = os.path.getsize(path)
    except OSError:
        return out
    limit = min(size, max_bytes)
    chunk_off = 0
    carry, carry_off = b"", 0
    try:
        with open(path, "rb") as f:
            while chunk_off < limit:
                raw = f.read(min(_SCAN_CHUNK, limit - chunk_off))
                if not raw:
                    break
                buf = carry + raw
                buf_off = carry_off
                chunk_off += len(raw)
                pos = 0
                while True:
                    nl = buf.find(b"\n", pos)
                    if nl < 0:
                        break
                    _collect_marker_line(out, pairs, buf_off + pos, buf[pos:nl])
                    pos = nl + 1
                carry, carry_off = buf[pos:], buf_off + pos
            if carry:
                _collect_marker_line(out, pairs, carry_off, carry)
    except OSError as exc:
        # 读到一半失败（文件被客户端整文件锁 / 中途被轮转）时，退回**已扫到的部分**。
        # 理由必须写出来：诊断函数抛错会让上层连接失败路径直接崩，比「证据少一点」糟得多。
        swallow(exc, why="读客户端日志中断：本函数退化为「仅返回已扫到的部分」，"
                        "绝不因读日志失败而让诊断抛错（诊断自身崩溃比诊断不到更糟）")
    return out


def _collect_marker_line(out: dict, pairs, offset: int, bl: bytes) -> None:
    """把一行（bytes）收进命中的标记桶（保留最后 _SCAN_KEEP_LINES 条）。"""
    hit = [m for m, mb in pairs if mb in bl]
    if not hit:
        return
    txt = _decode_bytes(bl).strip()
    for m in hit:
        bucket = out[m]
        bucket.append((offset, txt))
        if len(bucket) > _SCAN_KEEP_LINES:
            del bucket[0]


def _read_at(path: str, offset: int, max_bytes: int) -> str:
    """从绝对偏移读一段文本（容错解码；失败返回 ''）。"""
    if offset < 0:
        offset = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read(max_bytes)
    except OSError:
        return ""
    return _decode_bytes(raw)


def read_client_auth_flags(trade_dir: str) -> dict:
    """读回券商下发给客户端的最新「模块授权」开关（读日志，只读无副作用）。

    ★ 2026-10-09（R22）修复读取缺陷：旧实现只读**头 512KB / 尾 256KB**两个固定窗口，
      而授权串偏移随当天日志增长而增大（实测 17.2MB 的日志里授权串在 **1.57MB** 处，
      两个窗口都覆盖不到）⇒ 同一台机器「早上诊断正确、晚上静默失效」，且不报错、
      只表现为「无明确证据」。现改为全文件按标记扫描，取**最后一次**下发的授权串
      （重登会再下发一次，最后一次才是当前生效值），再从该偏移按窗口读取 JSON 原文。

    返回 {"found": bool, "flags": {键: 值}, "strict_xttrader": int|None,
          "strict_xtdata": int|None, "no_pid_check": int|None,
          "xtquant_auth": int|None, "gt_ipc_pair": int|None, "log": str}。
    """
    out: dict = {"found": False, "flags": {}, "strict_xttrader": None,
                 "strict_xtdata": None, "no_pid_check": None,
                 "xtquant_auth": None, "gt_ipc_pair": None, "log": ""}
    p = _newest_client_log(trade_dir)
    if not p:
        return out
    out["log"] = p
    hits = _scan_marker_lines(p, (_AUTH_MARKER,)).get(_AUTH_MARKER) or []
    if not hits:
        return out
    off, _line = hits[-1]
    seg = _read_at(p, off, _AUTH_SEG_BYTES)
    flags = {k: int(v) for k, v in _AUTH_SNIPPET_RE.findall(seg)}
    if not flags:
        return out
    out["found"] = True
    out["flags"] = flags
    out["strict_xttrader"] = flags.get(_AUTH_STRICT_KEYS[0])
    out["strict_xtdata"] = flags.get(_AUTH_STRICT_KEYS[1])
    out["no_pid_check"] = flags.get(_AUTH_NO_PID_KEY)
    out["xtquant_auth"] = flags.get(_AUTH_XTQUANT_KEY)
    out["gt_ipc_pair"] = flags.get(_AUTH_IPC_PAIR_KEY)
    return out


def diagnose_trade_connect(trade_dir: str) -> dict:
    """诊断「交易连接 rc=-1」的客户端侧真实原因（只读客户端日志，无副作用）。

    返回 {"reason","title","evidence","steps","auth","log"}：
      reason == "xtquant_server_blocked"     → 客户端未获授权启动量化服务
                                               （`The XtQuantServer is not allowed to start.`）
      reason == "pid_not_allowed"            → 严格连接校验按 PID 拉黑了本平台进程
      reason == "strict_check"               → 授权串显示已开启严格校验，但本轮日志未见显式拒绝
      reason == "xtquant_module_unauthorized"→ 无拒绝行/无严格校验，但 mdl_auth_xtquant=0
      reason == ""                           → 无明确证据（上层保持原有四步排查文案，不臆测）

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
    """单目录诊断（diagnose_trade_connect 的实现体，不做同根回退）。

    ★ 2026-10-09（R22）：取证从「尾 256KB + 末 1200 行」改为**全文件标记扫描**。
      旧实现的拒绝行 `The XtQuantServer is not allowed to start.` 落在当日日志
      1.81MB / 15.54MB 处，尾窗口（17.2MB - 256KB = 16.9MB 起）同样覆盖不到
      ⇒ 明明有确凿证据却报「无明确证据」。
    ★ 归因分流（同一句 "not allowed" 下藏两种完全不同的根因，不可混为一谈）：
      · `The XtQuantServer is not allowed to start.`（+ `mdl_auth_gt_ipc_pair=0`）
        → **客户端未获授权启动量化服务**（xtquant 模块授权未下发）；
      · `quant session N, pid X not allowed, return`
        → **严格连接校验 / PID 白名单**把调用进程拉黑。
    """
    res: dict = {"reason": "", "title": "", "evidence": [], "steps": "",
                 "auth": {}, "log": ""}
    auth = read_client_auth_flags(trade_dir)
    res["auth"] = auth
    res["log"] = auth.get("log") or ""
    p = auth.get("log") or _newest_client_log(trade_dir)
    if not p:
        return res
    res["log"] = p
    found = _scan_marker_lines(
        p, (_SERVER_BLOCKED_MARKER, "connect ret error", "not allowed"))
    blocked = found.get(_SERVER_BLOCKED_MARKER) or []
    connect_err = [t for _o, t in (found.get("connect ret error") or [])]
    denies = [t for _o, t in (found.get("not allowed") or [])]
    pid_denied = [t for t in denies if _PID_DENY_RE.search(t)]
    if blocked:
        res["reason"] = "xtquant_server_blocked"
        res["title"] = (
            "客户端**未获授权启动量化服务**：日志出现 "
            f"`{_SERVER_BLOCKED_MARKER}`（授权串 "
            f"{_AUTH_IPC_PAIR_KEY}={auth.get('gt_ipc_pair')}、"
            f"{_AUTH_XTQUANT_KEY}={auth.get('xtquant_auth')}）⇒ "
            "XtQuantServer 不监听，任何外部进程 connect() 必失败。")
        res["evidence"] = ([blocked[-1][1][-160:]]
                           + [f"授权串: {_AUTH_XTQUANT_KEY}="
                              f"{auth.get('xtquant_auth')}, "
                              f"{_AUTH_IPC_PAIR_KEY}={auth.get('gt_ipc_pair')}"]
                           + connect_err[-1:])[:3]
    elif pid_denied:
        res["reason"] = "pid_not_allowed"
        res["title"] = (
            "客户端启用了「量化连接严格校验」，拒绝了本平台进程的 xtquant 连接"
            f"（客户端日志：{pid_denied[-1][-140:]}）")
        res["evidence"] = (pid_denied[-2:] + connect_err[-1:])[:3]
    elif (auth.get("strict_xttrader") == 1
          and auth.get("no_pid_check") in (0, None)):
        res["reason"] = "strict_check"
        res["title"] = (
            "客户端授权串显示已开启「xttrader 严格连接校验」"
            f"（{_AUTH_STRICT_KEYS[0]}=1，{_AUTH_NO_PID_KEY}={auth.get('no_pid_check')}）")
        res["evidence"] = connect_err[-2:]
    elif (auth.get("xtquant_auth") == 0
          and auth.get("strict_xttrader") != 1):
        # 无拒绝行、无严格校验，但 xtquant 模块授权为 0 —— 同样是「通道未开放」。
        res["reason"] = "xtquant_module_unauthorized"
        res["title"] = (
            f"客户端授权串显示 `{_AUTH_XTQUANT_KEY}={auth.get('xtquant_auth')}`"
            "（该资金账号未获 xtquant 模块授权），量化通道未开放。")
        res["evidence"] = [f"{_AUTH_XTQUANT_KEY}={auth.get('xtquant_auth')}, "
                           f"{_AUTH_IPC_PAIR_KEY}={auth.get('gt_ipc_pair')}"]
    if not res["reason"]:
        return res
    # ★ 按根因给**不同的首句**：三种 reason 的处置重点不同，套同一段「加白名单」文案
    #   会把「模块未授权」的用户推去做无用功（与「假告状」同源的误导）。
    _lead = {
        "xtquant_server_blocked":
            "本机**不是**被 PID 白名单拒绝，而是客户端未获授权启动量化服务 —— "
            "请勿按「加白名单」方向排查，重点是「申请 xtquant 模块授权」。\n",
        "xtquant_module_unauthorized":
            "日志中**没有任何拒绝行、也没有严格校验**，纯粹是模块授权未下发 ⇒ "
            "重点是「申请 xtquant 模块授权」；\n",
    }.get(res["reason"], "")
    res["steps"] = (
        _lead
        + "该开关由券商服务端下发的模块授权控制（xttrader/xtdata "
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
    '_AUTH_MARKER',
    '_AUTH_XTQUANT_KEY',
    '_AUTH_IPC_PAIR_KEY',
    '_SERVER_BLOCKED_MARKER',
    '_PID_DENY_RE',
    '_scan_marker_lines',
    '_read_at',
    '_decode_bytes',
    '_client_log_dirs',
    '_newest_client_log',
    '_tail_text',
    '_head_text',
    '_read_window',
    'read_client_auth_flags',
    'diagnose_trade_connect',
    '_diagnose_one',
]
