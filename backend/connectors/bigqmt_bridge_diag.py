"""BigQmt 桥的**诊断面**：connector_probe / capability_gap / 根因四分法。

从 ``connectors/bigqmt_bridge.py`` 拆出（2026-10-15：本体新增
``capability_gap`` 后越过单文件 50KB 门限，``check_execution_architecture``
Gate 4；与 2026-10-01 gateway 面拆到 ``bigqmt_gateway.py`` 同型）。

以 **mixin** 形态挂回 ``BigQmtBridge``：方法仍是桥的实例方法，所有调用方
（``gateway.health`` / ``xtquant_client.manager`` / ``routes/broker.py`` /
``test_bigqmt_bridge_face`` / ``test_broker_capability_health``）零改动。

★ 职责纪律：本模块只放**只读诊断** —— 零 IO、禁阻塞、禁建连、禁改连接状态。
   任何带副作用的动作都属于本体（``bigqmt_bridge.py``）。
★ 依赖的本体属性与方法（``connector`` / ``connector_key`` /
   ``_agent_unresponsive`` / ``_liveness_failures`` / ``_last_agent_ok`` /
   ``_want_quotes`` / ``_applied_quotes`` / ``_quote_sync_error`` +
   ``pump_running`` / ``is_connected`` / ``_agent_quote_count``）由
   ``BigQmtBridge`` 提供。本模块**不 import 本体**（避免成环），纯鸭子类型。
"""
from __future__ import annotations

import time


class _BridgeDiagMixin:
    """诊断簇 mixin。方法语义与拆分前逐字一致（含全部注释与踩坑记录）。"""

    def connector_probe(self) -> dict:
        """桥接连接的诊断探针面（供 /brokers/diagnostics 与 health 消费）。

        只读本地缓存（transport.agent_meta 来自最近一次响应信封），**不做 IO** ——
        health 面禁止阻塞。agent 未回报过时诚实给空 dict。
        """
        tp = getattr(self.connector, "transport", None)
        out: dict = {
            "connector_key": self.connector_key,
            "transport": getattr(tp, "transport_id", "") or "",
        }
        # 本连接器**能翻译**的 canonical op 全量清单（由 dialect 自己报，不手抄）：
        # 排障时先看它，再看 agent 的 actions —— 两端数量对不上就是契约漂移。
        ops = getattr(self.connector, "supported_ops", None)
        if callable(ops):
            try:
                out["supported_ops"] = list(ops() or [])
            except Exception:  # noqa: BLE001  诊断面失败不得影响主流程
                pass
        meta = getattr(tp, "agent_meta", None)
        if callable(meta):
            out["agent"] = dict(meta() or {})
        elif isinstance(meta, dict):
            out["agent"] = dict(meta)
        sem = getattr(self.connector, "event_semantics", None)
        if callable(sem):
            try:
                spec = sem()
                out["event_semantics"] = getattr(getattr(spec, "semantics", None), "value", "") or ""
                out["max_latency_ms"] = int(getattr(spec, "max_latency_ms", 0) or 0)
            except Exception:  # noqa: BLE001  诊断面失败不得影响主流程
                pass
        # M2.7 排障四分法的关键字段上浮：路径不一致（agent 回报的 bridge_dir 与
        # 本地比对）、token 错（能收到响应即已排除）、函数缺失（funcs 清单）、
        # agent 未运行（meta 为空）。其余原始细节保留在 out["agent"] 里。
        agent = out.get("agent") or {}
        if agent:
            out["agent_bridge_dir"] = agent.get("bridge_dir", "")
            out["callback_bound"] = bool(agent.get("callback_bound"))
            out["direction_unknown"] = int(agent.get("direction_unknown") or 0)
            out["actions"] = list(agent.get("actions") or [])
            out["subscribed"] = int(agent.get("subscribed") or 0)
            out["ascii_only"] = bool(agent.get("ascii_only"))
        out["clock_offset_ms"] = int(getattr(tp, "clock_offset_ms", 0) or 0)
        out["pump_running"] = self.pump_running()
        # ---- 存活面：让「曾经连上」与「现在可用」在诊断里可区分 ----
        # 光看 connected 会误判（旧 bug 正是如此），因此把两个原始量都上浮：
        #   * agent_unresponsive：探活连续无应答（is_connected 已据此转 False）；
        #   * last_agent_ok_age_s：距上一次**成功往返**多少秒（None = 从未）。
        out["agent_unresponsive"] = bool(self._agent_unresponsive)
        out["liveness_failures"] = int(self._liveness_failures)
        out["last_agent_ok_age_s"] = (
            round(time.monotonic() - self._last_agent_ok, 1)
            if self._last_agent_ok else None)
        out["available"] = bool(self.is_connected())

        # ---- 行情订阅对账面：区分「本端要了什么」「agent 收到了什么」----
        # 这两者不一致 = 界面会停在种子价，而连接一切正常。不给出来就只能靠
        # 人盯盘口猜（``sync`` 的订阅日志永远是成功的）。
        out["quote_wanted"] = len(self._want_quotes)
        out["quote_applied"] = len(self._applied_quotes)
        out["quote_agent_subscribed"] = self._agent_quote_count()
        out["quote_sync_error"] = self._quote_sync_error

        # ---- 四类根因判定：**在服务端算好**，不让用户/前端自己比路径 ----
        # 「bridge_dir 两端不一致」是最高频的一类，而人眼比对两个长路径极易看漏
        # （大小写、正/反斜杠、末尾分隔符、\\ 长路径前缀都会造成「看着一样但不同」）。
        # 这里给出 normcase+normpath 后的布尔结论 + 一条可直接照做的引导。
        local_dir = str(getattr(tp, "bridge_dir", "") or "")
        out["local_bridge_dir"] = local_dir
        out["bridge_dir_match"] = self._bridge_dir_match(local_dir, agent)
        # ★ root_cause 单独兜底：它是最容易因 agent 字段形态意外而抛异常的一段
        #   （2026-10-12：int(list) TypeError 曾把整个 bigqmt 诊断块换成
        #   {"error": ...}，can_submit/available 全部陪葬）。失败时只降级这一个
        #   字段，其余诊断字段照常上浮。
        try:
            out["root_cause"] = self._root_cause(agent, out["bridge_dir_match"])
        except Exception as exc:  # noqa: BLE001  单字段降级，不拖垮整面
            out["root_cause"] = ""
            out["root_cause_error"] = "root_cause 计算失败（%s），其余字段可信" % exc
        # ---- 下单能力（can_submit）：**唯一**判据是终端注入的 ``passorder`` 是否被捕获 ----
        # ★ 与 ``available``（桥往返可用 = 只读可用）语义**不同**，必须分开暴露：
        #   独立进程模式下 agent 能收发请求（available=True）却拿不到 passorder
        #   （can_submit=False）—— 行情/查询可用、下单不可用。只给一个 available
        #   会让「连接绿灯」被读成「能下单」，正是假绿灯家族最常见的形态。
        # ★ 转发面（xtdata 行情函数，如 get_full_tick）**不含** passorder ⇒ 不会被转发污染。
        out["can_submit"] = "passorder" in (agent.get("funcs") or [])
        return out

    #: 「桥可用但下不了单」时给出的处置文案（供 gateway.health 与前端复用同一份口径）。
    _NO_TRADE_REMEDY = (
        "大 QMT 桥已连通，但 agent **未捕获到终端注入的 passorder** ⇒ 本连接只能查"
        "行情/K线，**无法下单**。典型成因：该策略被 QMT 登记成了「独立进程 / 原生"
        "Python」（终端只把 passorder / get_trade_detail_data 注入**进程内**被公式"
        "引擎挂载的那个文件）。处置：在 QMT 策略编辑器里把它改成「内置Python」形态并"
        "重新编译，然后重启策略；用 `python scripts/qmt_agent_deploy.py check` 自证"
        "运行形态。")

    def capability_gap(self) -> str:
        """**O(1) 只读**：桥可用但不具备下单能力时返回可执行的原因，否则返回 ""。

        为什么要单独一个方法（2026-10-14 真机取证）
        ------------------------------------------
        桥的存活判据（``is_connected``：``_connected and not _agent_unresponsive``）
        只回答「agent 还在应答吗」，**不回答「agent 能下单吗」**。本机实测状态是
        「心跳新鲜 + PROBE 通过 + 桥显示已连接」而 ``injected=[]``、
        ``trade_surface.can_submit=false`` —— 界面上是一条**绿灯**，用户每次操作却
        撞 503。这正是本项目反复出现的「假绿灯」家族：存活 ≠ 可用。

        本方法给 ``gateway.health`` 一个**零 IO** 的能力判据（复用
        ``connector_probe`` 的本地缓存，不额外往返），使 ``health_status`` 能如实
        降级为 ``needs_action`` 并带上处置文案。

        三种返回语义必须区分：
          * agent 尚未回报 meta（``agent`` 为空）→ ``""``（**未知**，不妄断，否则
            会在刚连上的一瞬间把一切正常的连接误报成不可下单）；
          * 已回报且含 ``passorder`` → ``""``（具备下单能力）；
          * 已回报但不含 ``passorder`` → 处置文案（**确知**不可下单）。
        """
        try:
            probe = self.connector_probe()
        except Exception:  # noqa: BLE001  诊断面失败不得影响健康判定
            return ""
        agent = probe.get("agent") or {}
        if not agent:
            return ""           # 未回报 ⇒ 未知，绝不冒充「不可下单」
        if probe.get("can_submit"):
            return ""
        mode = str(agent.get("runtime_mode") or "")
        extra = ("（agent 自报运行形态：%s）" % mode) if mode else ""
        return self._NO_TRADE_REMEDY + extra

    @staticmethod
    def _bridge_dir_match(local_dir: str, agent: dict) -> bool | None:
        """本地 bridge_dir 与 agent 回报值是否同一目录；无法判定时返回 None。

        None 的语义是「**还不知道**」（agent 尚未回报 meta），与 False
        （「确实不一致」）必须区分 —— 把「未知」显示成「不一致」会让用户
        白折腾一次改配置。
        """
        if not agent:
            return None
        remote = str(agent.get("bridge_dir") or "")
        if not local_dir or not remote:
            return None
        try:
            import os
            return os.path.normcase(os.path.normpath(local_dir)) == \
                os.path.normcase(os.path.normpath(remote))
        except Exception:  # noqa: BLE001  路径非法不该让诊断面崩
            return local_dir == remote

    @staticmethod
    def _root_cause(agent: dict, dir_match: bool | None) -> str:
        """把排障四分法压成**一个**可展示的结论串（空串 = 未发现问题）。

        四分法：agent 未运行 / bridge_dir 两端不一致 / 下单函数缺失 / token 不匹配。
        顺序按「先看有没有连上，再看配置对不对」的排查习惯排列。

        ★ 交易能力（``passorder``）判据**先于** ``trading_enabled``（2026-10-12 修）。
          独立进程（standalone）模式下 agent 只**转发** xtdata 行情函数、**拿不到**
          终端注入的 ``passorder`` —— 此时把 ``trading_enabled`` 置 true 也**下不了单**。
          旧实现只判「funcs 是否为空」，而转发面本就非空 ⇒ 漏判，把根因误报成
          ``trading_enabled=false``，引导用户去改一个改不好的开关（假绿灯家族：
          按它排查永远查不到，因为真实原因是模式不对）。现改为先看 ``passorder``。
        """
        if not agent:
            return "agent 未运行或从未应答（桥目录里没有任何响应信封）"
        if dir_match is False:
            return ("bridge_dir 两端不一致：agent 读的是另一个目录，"
                    "请求永远等不到响应。请让 agent_config.json 与连接配置指向同一目录")
        funcs = agent.get("funcs") or []
        # ★ ``passorder`` 判据**先于**一切（2026-10-12 修）。
        #   ``funcs`` 为空是它的**子情形**（空列表当然不含 passorder），不是并列分支：
        #   转发来的 xtdata 行情函数已按 ``forwarded`` 单列、不再冒充注入 ⇒ funcs 为空
        #   恰恰说明「一个终端函数都没拿到」，归因必须落到**运行模式不对**
        #   （独立进程）。旧文案「终端版本不提供这些函数」是版本背锅式误导——
        #   同一版本下公式策略就能拿到，问题在挂载形态而非版本。
        if "passorder" not in funcs:
            # ★ ``forwarded_funcs`` 在 agent meta 里是 **list**（sorted(extra.keys())，
            #   「弱证据，仅供诊断」）；老 bundle / 不同版本可能给 int 或不给。
            #   2026-10-12 实测教训：这里直接 int(list) 抛 TypeError，被 health 端点
            #   的兜底整块替换成 {"error": ...} ⇒ can_submit/root_cause 全部不可见
            #   ——「一个诊断字段类型意外」演变成「整个诊断面报废」。
            raw_fwd = agent.get("forwarded_funcs")
            if isinstance(raw_fwd, (list, tuple, set)):
                forwarded = len(raw_fwd)
            elif isinstance(raw_fwd, int):
                forwarded = raw_fwd
            else:
                forwarded = 0
            if not funcs:
                return ("agent 已就绪但**一个终端注入函数都没有**（只转发了 %d 个 xtdata "
                        "行情接口）——当前以**独立进程**运行，下单 / 撤单 / 账户查询均不可用。"
                        "把 agent_config.json 的 trading_enabled 置 true **无效**；"
                        "需让本策略以「公式策略」形态被 QMT in-process 挂载"
                        "（同系统自带策略），而不是以外部 Python 脚本策略运行" % forwarded)
            return ("agent 已捕获 %d 个注入函数但**没有 passorder** —— 当前以**独立进程**运行"
                    "（终端未把交易函数注入），下单 / 撤单 / 账户查询均不可用。"
                    "把 agent_config.json 的 trading_enabled 置 true **无效**；"
                    "需让本策略以「公式策略」形态被 QMT in-process 挂载（同系统自带策略）"
                    % len(funcs))
        if not agent.get("trading_enabled"):
            return ("agent 已连通且已捕获 passorder，但 trading_enabled=false："
                    "只读可用，下单会被 agent 拒绝（确认无误后在 agent_config.json 置 true）")
        return ""
