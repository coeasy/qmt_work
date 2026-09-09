"""XTPQuantAdapter 主类组装（自原 xtp.py 逐行搬移；行情/账户/交易/合约方法经 Mixin 组合）。"""

import os
import threading

from ..base import BrokerAdapter, BrokerNotConnectedError, BrokerSDKError
from ._common import _load_trader_api, _load_xtquant_from, _resolve_xtquant_path, _shell_attr, log
from .account import AccountMixin
from .env import (
    QmtVersionProfile,
    _effective_trade_dir,
    _latest_login_log,
    _probe_xtdata,
    _running_client_exes,
    build_version_profile,
    probe_environment,
)
from .instrument import InstrumentMixin
from .quotes import QuotesMixin
from .trading import TradingMixin


class XTPQuantAdapter(QuotesMixin, AccountMixin, TradingMixin, InstrumentMixin, BrokerAdapter):
    """迅投 XTQuant 真实适配器。"""

    def __init__(self, client_path: str, account_id: str, account_type: str = "STOCK",
                 session_id: int = 0, min_version: str = "", client_mode: str = "auto"):
        self.client_path = client_path
        # 客户端连接模式：auto（自动推断）/ mini（极速版 MiniQMT，userdata_mini）/
        # full（完整版大客户端，userdata）。决定交易侧 XtQuantTrader 使用的数据目录。
        self._client_mode = (client_mode or "auto").lower()
        self._account_id = account_id
        self._account_type = (account_type or "STOCK").upper()
        self.session_id = int(session_id or 0)
        self.min_version = min_version
        self._xtdata = None
        self._trader = None
        self._acc = None
        self._connected = False
        self._lock = threading.Lock()  # 交易调用串行锁（SDK 侧互斥）
        # 回调锁：专门保护 seq→oid / oid→seq / pending 三张映射（SDK 回调线程与
        # 业务线程并发访问）。与 _lock 分离，避免「锁内调 SDK 方法 + SDK 同步派发
        # 回调再取同一把锁」造成死锁。
        self._map_lock = threading.Lock()
        self._order_status_cache: dict[str, str] = {}
        self._name_cache: dict[str, str] = {}
        # 阶段 0-A：报单回调闭环 —— 本地 seq → 柜台真实 order_id 双向映射，
        # 以及 place_order 等待 response 回调的 pending 表。
        self._seq_to_oid: dict[int, str] = {}
        self._oid_to_seq: dict[str, int] = {}
        self._pending_resp: dict[int, tuple[threading.Event, list]] = {}
        # 外部钩子（由 sync 引擎 / manager 注入，用于把回报直推行情/对账管线）
        self._on_order_cb = None
        self._on_trade_cb = None
        self._on_disconnect_cb = None
        self._probe_cache = None  # 版本画像复用：连接期探测的运行场景结果缓存

    # ---------------- 身份 ----------------
    @property
    def broker_name(self) -> str:
        return "迅投XTQuant"

    @property
    def adapter_id(self) -> str:
        return "xtp"

    @property
    def client_version(self) -> str:
        ver = self.min_version or "xtquant"
        if self._xtdata is not None:
            try:
                return getattr(self._xtdata, "__version__", ver) or ver
            except Exception:  # noqa: BLE001
                return ver
        return ver

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def account_type(self) -> str:
        return self._account_type

    @property
    def supported_periods(self) -> list[str]:
        # 展示层用平台命名（"60m"）；真实调用经 get_kline/subscribe_quote 内的
        # _normalize_kline_period 归一化为迅投协议周期（"60m"->"1h"）。
        return ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mon"]

    @property
    def supported_account_types(self) -> list[str]:
        return ["STOCK", "CREDIT", "OPTION", "FUTURES"]

    @property
    def sdk_required(self) -> str:
        return "xtquant"

    def capabilities(self) -> list[str]:
        """运行时能力探测：按当前账号 / 目录 / 连接状态推导能力矩阵。

        供 Registry.probe 优先采用（优于基于券商档案的静态推导），前端据此
        展示「当前 QMT 版本实际支持哪些功能」。连接失败/未连接时不臆造能力。
        """
        try:
            return self.version_profile().capabilities.as_list()
        except Exception:  # noqa: BLE001
            return []

    def version_profile(self) -> QmtVersionProfile:
        """返回当前客户端安装的版本画像（类型 + 版本 + 能力矩阵）。

        复用连接期已解析的交易目录 / 模式；运行进程探测优先复用适配器内缓存，
        未缓存时现测（诊断路径，无副作用）。
        """
        probe = getattr(self, "_probe_cache", None)
        acc_filled = bool(self._account_id)
        return build_version_profile(
            self.client_path, self._client_mode,
            account_id=self._account_id, account_type=self._account_type,
            realtime_push=self._connected and acc_filled,
            probe=probe)

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        # 幂等：已连接则复用，避免重复 start 泄漏第二个交易会话（同账号多会话冲突）
        if self._connected:
            return
        # 自动发现并加载客户端自带的 xtquant（无需用户手动 pip install）
        xtq_sp = _resolve_xtquant_path(self.client_path)
        if xtq_sp:
            try:
                _load_xtquant_from(xtq_sp)
                log.info("xtquant auto-loaded from client dir: %s", xtq_sp)
            except Exception as exc:  # noqa: BLE001
                raise BrokerSDKError(
                    "xtquant",
                    f"客户端目录已发现 xtquant（{xtq_sp}）但导入失败：{exc}") from exc
        try:
            import xtquant.xtdata as xtdata  # noqa: F401
            XtQuantTrader, _acc_classes = _load_trader_api()
        except Exception as exc:  # noqa: BLE001
            if xtq_sp:
                raise BrokerSDKError(
                    "xtquant",
                    f"客户端目录已发现 xtquant（{xtq_sp}）但导入失败：{exc}") from exc
            raise BrokerSDKError(
                "xtquant",
                "未找到 xtquant：已自动在客户端目录（bin.x64\\Lib\\site-packages）搜索失败，"
                "请确认「券商连接」填写的 client_path 指向客户端数据目录"
                "（极速版 userdata_mini / 完整版 userdata）且客户端已安装登录，"
                "或手动 pip install xtquant") from exc

        self._xtdata = xtdata
        xtdata_ok, xtdata_detail = _probe_xtdata(xtdata, self.client_path)

        if not self._account_id:
            # 仅行情模式（无交易账号）：行情可用，交易不可用。
            # 在 start() 阶段就给出明确指引，而不是等到 get_quote 才抛 SDK 原话
            #「无法连接行情服务！」——那种报错会让用户误以为是平台 bug。
            # 阶段 5：恒生UF 等定制版的诊断细化——区分「客户端未登录」「客户端未运行」
            # 「客户端路径错误」等场景，给出可操作指引。
            if not xtdata_ok:
                # 根据 xtdata_detail 关键字推断场景（无需修改 SDK）
                low = (xtdata_detail or "").lower()
                if "未登录" in xtdata_detail or "login" in low or "auth" in low:
                    scene = "客户端未登录（行情/交易服务尚未登录）"
                    steps = (
                        "1) 在 QMT 客户端中完成<b>行情 + 交易</b>登录（极速/普通模式均可）；\n"
                        "2) 确认客户端界面能正常显示行情（否则 SDK 无法连接）；\n"
                        "3) 保持客户端运行，再重试连接。")
                elif "未启动" in xtdata_detail or "not running" in low or "not started" in low:
                    scene = "客户端未启动"
                    steps = (
                        "1) 启动 QMT 客户端（恒生UF：HsUFTrader / UFClient）；\n"
                        "2) 完成登录后保持客户端运行；\n"
                        "3) 再次点击连接。")
                elif "端口" in xtdata_detail or "port" in low or "rpc" in low:
                    scene = "客户端 RPC 端口未就绪"
                    steps = (
                        "1) 重启 QMT 客户端；\n"
                        "2) 确认客户端登录后无防火墙拦截；\n"
                        "3) 再次点击连接。")
                else:
                    # 深度诊断：主动探测进程 / 端口 / 配置一致性，替代笼统的「不可用」
                    probe = _shell_attr("_probe_quote_service")(self.client_path)  # monkeypatch 兼容：经壳模块动态查找（见 _common._shell_attr）
                    self._probe_cache = probe  # 供版本画像复用，避免重复探测
                    _probe_diag = (f"[探测] client_type={probe.get('client_type')} "
                                   f"full={probe.get('full_client_running')} "
                                   f"mini={probe.get('mini_client_running')} "
                                   f"port_map={probe.get('port_map')} "
                                   f"quote_ports={probe.get('quote_ports')} "
                                   f"trade_ports={probe.get('trade_ports')} "
                                   f"cfg_root={probe['cfg_root']} "
                                   f"cfg_root_exists={probe['cfg_root_exists']} "
                                   f"cfg_matches={probe['cfg_matches']}")
                    log.info("%s", _probe_diag)
                    if not probe["client_running"]:
                        scene = "QMT 客户端未运行"
                        steps = (
                            "1) 打开并登录 QMT 客户端（大窗口或小窗口均可），保持客户端运行；\n"
                            "2) 确认客户端界面能正常显示行情；\n"
                            "3) 再重试连接。")
                    elif (probe.get("full_client_running")
                          and not probe.get("quote_service_ok")
                          and not probe.get("mini_client_running")):
                        scene = ("大窗口客户端（XtItClient）已登录，"
                                 "但小窗口行情服务（miniquote）未运行")
                        steps = (
                            "1) xtdata 行情接口由<b>小窗口</b>（miniQmt / 极简模式）提供，"
                            "大窗口的 58600 端口仅支持交易，不支持行情 RPC；\n"
                            "2) 方案A：在大窗口客户端中开启「独立行情 / 极简模式」"
                            "（会自动拉起 miniquote 行情服务）；\n"
                            "3) 方案B：直接运行客户端目录 bin.x64\\XtMiniQmt.exe 并完成登录"
                            "（部分券商首次需输入验证码）；\n"
                            "4) 小窗口就绪后（端口 58610 监听），无需重启平台，直接重试连接。")
                    elif (probe.get("full_client_running")
                          and probe.get("mini_client_running")
                          and not probe.get("quote_service_ok")):
                        scene = "大小窗口均在运行，但行情端口未监听（miniquote 未就绪）"
                        steps = (
                            "1) 小窗口可能停留在登录界面（含验证码）尚未完成登录；\n"
                            "2) 请在弹出的 XtMiniQmt 登录窗口输入账号/密码/验证码；\n"
                            "3) 登录成功后 miniquote 将监听 58610，再重试连接。")
                    elif not probe["listening_ports"]:
                        scene = f"客户端在运行，但行情端口 {probe['expected_port']} 未监听"
                        steps = (
                            "1) 客户端已在运行，但行情服务端口未就绪；\n"
                            "2) 请确认 QMT 已完成<b>行情登录</b>（部分券商需单独登录行情）；\n"
                            "3) 确认客户端行情图正常刷新后，重启客户端再重试连接。")
                    elif probe["cfg_matches"] is False:
                        if probe.get("cfg_root_exists") is False:
                            scene = "行情配置为旧残留，指向已不存在的客户端目录"
                            steps = (
                                f"1) 检测到残留配置 {os.path.join(os.environ.get('USERPROFILE', ''), '.xtquant', '*', 'xtdata.cfg')} 指向不存在的目录：{probe['cfg_root']}；\n"
                                "2) 这是旧 QMT 客户端（如机构版 jigou_qmt）卸载后遗留的配置；\n"
                                "3) 请删除 %USERPROFILE%\\.xtquant\\ 下对应 guid 目录中的 xtdata.cfg，\n"
                                "   或重启当前 QMT 客户端（gd_qmt）使其重新生成正确配置，再重试连接。")
                        else:
                            scene = "行情配置指向了其他 QMT 客户端"
                            steps = (
                                f"1) 检测到行情配置指向：{probe['cfg_root']}，与当前客户端路径不一致；\n"
                                "2) 请确认本机只运行与「客户端路径」匹配的那一个 QMT；\n"
                                "3) 若本机装有多个 QMT（如广发/机构版），请运行路径对应的那个并重新登录。")
                    else:
                        scene = "行情服务不可用"
                        steps = (
                            "1) 打开并登录 QMT 客户端（极速/普通模式均可），保持客户端运行；\n"
                            "2) 确认客户端能正常显示行情；\n"
                            "3) 确认「客户端路径」与「客户端模式」匹配（极速版 userdata_mini / 完整版 userdata）。")
                raise BrokerNotConnectedError(
                    f"行情服务连接失败（{scene}，SDK 返回：{xtdata_detail}）。\n"
                    f"请按顺序排查：\n{steps}\n{_probe_diag}")
            self._connected = True
            return

        # 交易数据目录：按客户端模式解析（极速版 userdata_mini / 完整版 userdata）。
        # 行情侧 xtdata 走固定 58610，与此无关；但 XtQuantTrader 必须指向正确的数据目录，
        # 否则「完整版大客户端配了 userdata_mini」会连不上交易（反之亦然）。
        trade_dir, resolved_mode = _effective_trade_dir(self.client_path, self._client_mode)
        if resolved_mode in ("mini", "full"):
            log.info("XtQuantTrader 交易目录按客户端模式解析：%s -> %s（模式 %s）",
                     self.client_path, trade_dir, resolved_mode)

        try:
            # session 占用规避：连接失败时递增 session_id 重试（0..5），
            # 规避用户已手动打开 QMT 客户端占用默认 session 的场景。
            #
            # 关键兼容性（历史 bug）：xtquant 的 XtQuantTrader.start() 在多数版本里
            # **没有返回值**（源码 `self.async_client.start(); return`），因此 rc 为
            # None。旧实现用 `if rc == 0` 判断启动成功，None == 0 恒为 False，导致
            # 交易模式在这些 SDK 版本下 100% 连不上（无论客户端是否已登录），且报错
            # 指向「session 被占用」误导排查方向。
            # 正确语义：start() 返回 None 视为已启动；真正的连接结果由 connect()
            # 决定（返回 0 成功，非 0 失败——session 冲突在这里暴露）。
            # 候选交易目录：首选按客户端模式解析的目录；失败时自动降级尝试另一模式
            # 目录（完整版 userdata <-> 极速版 userdata_mini 互备）。很多券商同一路径
            # 下既有完整版又有极速版（userdata + userdata_mini 并存），当前解析模式
            # 连不上并不代表另一模式也连不上——例如完整版大客户端未以「极简模式」登录、
            # 而极速版 miniQMT 已登录的情况。故按序尝试，命中即成功。
            candidate_dirs = [trade_dir]
            _alt_dir, _alt_mode = _effective_trade_dir(
                self.client_path, "mini" if resolved_mode != "mini" else "full")
            if _alt_dir and _alt_dir != trade_dir and os.path.isdir(_alt_dir):
                candidate_dirs.append(_alt_dir)
            trader = None
            last_err = ""
            used_dir = trade_dir
            resolved_mode_final = resolved_mode
            for d in candidate_dirs:
                if trader is not None:
                    break
                used_dir = d
                resolved_mode_final = ("mini" if d.endswith("userdata_mini") else "full")
                for attempt in range(6):
                    sid = self.session_id + attempt
                    t = XtQuantTrader(d, sid)
                    rc = t.start()
                    if rc is not None and rc != 0:
                        last_err = f"start rc={rc}"
                        continue
                    crc = t.connect()
                    if crc == 0:
                        trader = t
                        self.session_id = sid
                        break
                    last_err = f"connect rc={crc}"
                    # 释放本次失败的会话，避免连续重试泄漏多个 session
                    try:
                        t.stop()
                    except Exception:  # noqa: BLE001
                        pass
            trade_dir = used_dir
            if trader is None:
                _exe_procs = _running_client_exes()
                _login_log = _latest_login_log(trade_dir)
                # 多因子真实归因（替代旧版「一律归因程序化权限请联系券商」的误导文案）：
                # rc=-1 常见根因按官方排查顺序为 ①登录模式 ②路径 ③session ④权限。
                # 这里把已实测到的事实（运行进程 / 尝试的模式 + 目录互备 / client_mode）
                # 一并列出，让定位不再猜。
                tried = " → ".join(os.path.basename(x) for x in candidate_dirs)
                raise BrokerNotConnectedError(
                    f"交易连接失败（session_id {self.session_id}~{self.session_id + 5} "
                    f"均 {last_err}）。\n"
                    f"### 已实测的排查事实\n"
                    f"  1) 客户端进程：{_exe_procs or '未检测到'}；\n"
                    f"  2) 尝试的数据目录（按顺序）：{tried or (trade_dir or '（无）')}；\n"
                    f"  3) 配置客户端模式：client_mode={self._client_mode}"
                    f"，解析判定 @{resolved_mode_final}。\n"
                    f"### 官方四步排查（迅投 FAQ）\n"
                    f"  ① [极简模式] QMT 登录时是否勾选「极简模式」——完整版大客户端未以"
                    f"极简模式登录时，外部 API 交易连接（XtQuantTrader）会返回 rc=-1；\n"
                    f"  ② [路径/模式匹配] 极速版必须指向 <安装目录>\\userdata_mini，"
                    f"完整版指向 \\userdata；C 盘安装需以管理员权限运行连接端；\n"
                    f"  ③ [session] 换另一个 session_id 再试（同一 session 两次 connect "
                    f"间隔需 >3 秒）；\n"
                    f"  ④ [权限] 若以上均正确仍 rc=-1，才是【资金账号未开通 QMT "
                    f"「程序化交易/策略交易权限」】（仅「基础交易权限」不够），联系券商核实。\n"
                    f"建议优先：运行并登录极速版 bin.x64\\XtMiniQmt.exe（登录时勾选极简"
                    f"模式），或以极简模式重新登录完整版客户端后再连接。"
                    f"（客户端登录日志 {_login_log}）")
            resolved_mode = resolved_mode_final
            if self._account_type not in _acc_classes:
                raise BrokerNotConnectedError(
                    f"该客户端不支持账户类型 {self._account_type}（支持：{sorted(_acc_classes)}）")
            cls = _acc_classes[self._account_type]
            self._acc = cls(self._account_id, self._account_type)
            trader.subscribe(self._acc)
            # 阶段 0-A（C1）：注册回调，否则断线零感知、拿不到柜台 order_id 与成交回报。
            # XtQuantTraderCallback 随 xtquant 版本命名不同，兼容新旧两处导入。
            try:
                from xtquant.xt_trader import XtQuantTraderCallback
            except ImportError:
                from xtquant.xttrader import XtQuantTraderCallback
            try:
                trader.register_callback(self._make_callback(XtQuantTraderCallback))
            except Exception as exc:  # noqa: BLE001
                # 回调注册失败不应阻断连接；但记日志以便排查（影响断线感知/回报）
                log.warning("register_callback 失败（断线感知/成交回报将不可用）：%s", exc)
            self._trader = trader
            self._connected = True
        except BrokerNotConnectedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"交易连接异常：{exc}") from exc

    def probe(self) -> dict:
        """结构化环境诊断（不依赖连接）：供 /brokers/test 与 tools/diag_qmt.py 展示。"""
        diag = probe_environment(self.client_path)
        diag["session_id"] = self.session_id
        diag["account_id"] = self._account_id
        return diag

    def close(self) -> None:
        had_trader = self._trader is not None
        trader = self._trader
        # 阶段 0-A（C10）：先标记断开，再释放 trader 与映射，避免回调/竞态复用已停用句柄。
        self._connected = False
        if trader is not None:
            try:
                # 释放交易会话与端口。原实现仅把指针置 None，不调 stop()——
                # 反复连接/断开会累计残留 session/监听端口，最终同账号会话被占满，
                # 后续连接报「session 被占用」，表现为「连接不上 QMT」。
                trader.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._map_lock:
            self._seq_to_oid.clear()
            self._oid_to_seq.clear()
            self._pending_resp.clear()
        self._trader = None
        self._acc = None
        if had_trader:
            log.info("XTPQuantAdapter closed (session_id=%s)", self.session_id)

    def is_connected(self) -> bool:
        return self._connected

    def _require_trader(self):
        # 阶段 0-A（C10）：不仅检查对象存在，还要确认连接态——否则对已 stop 的
        # trader 继续调用 order_stock/query_asset 会拿到错误结果或崩溃。
        if not self._connected or self._trader is None or self._acc is None:
            raise BrokerNotConnectedError("交易未连接：未配置 account_id 或客户端未登录")
        return self._trader, self._acc


__all__ = [
    'XTPQuantAdapter',
]
