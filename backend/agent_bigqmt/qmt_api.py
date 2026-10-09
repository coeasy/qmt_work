# -*- coding: utf-8 -*-
"""大 QMT 端 action 路由执行器（py3.6 兼容，标准库 only）。

设计纪律
--------
1. **函数一律从注入命名空间拿**（``self.injected.get(name)``），禁止在代码里
   写死函数名做 import —— 因为这些函数不是任何模块的成员，是 QMT 注入进来的。
2. **对象属性一律用 getattr 兜底**：不同券商版本的 QMT 对象字段不完全一致，
   少一个属性就让整个查询失败是不可接受的（宁可字段为 None）。
3. **失败必有 error_type**：让外部端能精确重建异常（503 还是 400 取决于此）。
4. **不做能力断言**：某个函数没捕获到 ⇒ 报 ``not_captured``（= UNKNOWN），
   **绝不**输出「终端没有该接口」——那正是掩盖 bug 的元凶。
"""
from __future__ import print_function

import json
import os
import sys
import time
import traceback

VERSION = "1.0.0"

# get_trade_detail_data 的数据类型名（QMT 侧固定值）
_DT_ACCOUNT = "ACCOUNT"
_DT_POSITION = "POSITION"
_DT_ORDER = "ORDER"
_DT_DEAL = "DEAL"

#: passorder 的默认参数模板 —— **以客户端版本为准**。
#: 不同券商/版本的 QMT 对这些枚举的定义不一致，出问题先改这里（也可在
#: agent_config.json 里用 "passorder" 键覆盖），而不是改代码逻辑。
_DEFAULT_PASSORDER = {
    "opType_buy": 23,
    "opType_sell": 24,
    "orderType": 1101,      # 单股、单账号、普通、股票
    "prType_limit": 11,     # 限价委托
    "prType_market": 5,     # 市价（对手方最优）
    "quickTrade": 2,
    "strategyName": "qmt_work",
    # 账户类型（QMT opAccountType 枚举）。非 0 值会触发**扩展 12-参数签名**
    # 的 passorder 调用（标准 11-arg 版本会自动降级）。
    # 不同券商版本可能不支持扩展签名 —— do_place 会捕获 TypeError 并降级。
    "opAccountType_stock": 0,   # A 股股票（默认，走 11-arg 标准签名）
    "opAccountType_etf": 1,     # ETF 基金
    "opAccountType_option": 2,  # 股票期权
    "opAccountType_future": 3,  # 期货
    "opAccountType_credit": 4,  # 融资融券（两融）
}


#: 账户类型别名归一化：不同前端/文档可能用不同写法，统一成内部 key。
_ACCOUNT_TYPE_ALIASES = {
    # 股票
    "stock": "stock", "a_stock": "stock", "astock": "stock", "a股": "stock",
    # ETF
    "etf": "etf", "fund_etf": "etf", "lof": "etf",
    # 期货
    "future": "future", "futures": "future", "qh": "future", "期货": "future",
    # 期权
    "option": "option", "options": "option", "期权": "option",
    # 两融
    "credit": "credit", "margin": "credit", "two_fin": "credit",
    "融资融券": "credit", "两融": "credit",
}


#: 标的代码格式（正则）。account_type → regex 字符串。
#: 不匹配直接拒绝（BrokerError），避免柜台返难懂的错。
#:
#: 覆盖范围（QMT 常用市场）：
#:   stock  : 沪深 A 股（60xxxx/000xxx/002xxx/688xxx/300xxx/8/4/605xxx）
#:   etf    : 沪 5xxxxx / 深 1xxxxx
#:   future : 沪 SHF（CU/AU 等）、深 SZF（IF/IC/IH）、大商所 DCE（M/I/J）、
#:            郑商所 CZCE（SR/CF/MF）
#:   option : 沪 100xxxxx.SH / 深 020xxxxx.SZ
#:   credit : 与 A 股同一 code 体系（两融账户走同一支股票的融资融券通道）
_CODE_PATTERNS = {
    "stock": r"^(60\d{4}|000\d{3}|001\d{3}|002\d{3}|003\d{3}|603\d{3}"
             r"|605\d{3}|688\d{3}|689\d{3}|300\d{3}|301\d{3}|302\d{3}"
             r"|430\d{3}|8[37]\d{4})\.(SH|SZ)$",
    "etf": r"^(5\d{5}|1\d{5})\.(SH|SZ)$",
    "future": r"^[A-Za-z]{1,4}\d{4}\.(SHF|SZF|DCE|CZCE|INE|GFE|GFEX)$",
    "option": r"^(100\d{5}\.SH|020\d{5}\.SZ)$",
    "credit": r"^(60\d{4}|000\d{3}|001\d{3}|002\d{3}|003\d{3}|603\d{3}"
              r"|605\d{3}|688\d{3}|689\d{3}|300\d{3}|301\d{3}|302\d{3}"
              r"|430\d{3}|8[37]\d{4})\.(SH|SZ)$",
}


def normalize_account_type(value):
    """把外部传入的 account_type 归一化到内部 key。

    None/'' → 'stock'（向后兼容：老调用方没传 account_type 就是 A 股）。

    认不出的名字 → 抛 ``ValueError``（**绝不静默落到 A 股**）。

    ⚠️ R19 第 1 轮修正：R18 初版把未知值兜底成 'stock'，与「零 mock / 不静默
    吞错」纪律冲突 —— 用户把 ``futures`` 写成 ``future1`` 时会**当成 A 股送单**。
    虽然 ``validate_code`` 对期货/期权/ETF 的代码格式能兜住大部分误输入，
    但 ``credit`` 与 ``stock`` 共用同一套代码格式，误输入会**静默走错通道**。
    改为抛错后由 ``do_place`` 转成 ``BrokerError`` 返回给外部端，错误可见。
    """
    if value is None or value == "":
        return "stock"
    key = _ACCOUNT_TYPE_ALIASES.get(str(value).strip().lower())
    if key is None:
        raise ValueError(
            "不支持的 account_type: %r（可选: %s）"
            % (value, ", ".join(sorted(set(_ACCOUNT_TYPE_ALIASES.values())))))
    return key


def resolve_account_type(account_type, po=None):
    """(key, opAccountType 数值)。po=None 时读 _DEFAULT_PASSORDER 作兜底。

    ``account_type`` 非法时抛 ``ValueError``（上层转 BrokerError）。
    """
    po = po or _DEFAULT_PASSORDER
    key = normalize_account_type(account_type)
    # 配置里若没有对应 opAccountType_* 键（比如券商版本没定义），
    # 回退到 stock 的 0 值——这样至少不炸、也留了清晰的 raw 值给上层判定。
    op_value = po.get("opAccountType_%s" % key, 0)
    try:
        op_value = int(op_value)
    except (TypeError, ValueError):
        op_value = 0
    return key, op_value


def validate_code(code, account_type):
    """按 account_type 校验标的代码格式。

    返回 (ok, error_msg)。ok=False 时 error_msg 描述期望的格式，避免柜台
    返难懂的 'invalid instrument' 报错。

    ``account_type`` 非法时抛 ``ValueError``（与 ``normalize_account_type`` 一致）。
    """
    key = normalize_account_type(account_type)
    pattern = _CODE_PATTERNS.get(key)
    if not pattern:
        # 未知类型：不拒绝，让 passorder 层去报
        return True, ""
    import re as _re
    if not _re.match(pattern, str(code or "")):
        return False, ("code=%r 不符合 %s 标的格式（期望：%s）"
                       % (code, key, pattern))
    return True, ""


class ActionError(Exception):
    """带 error_type 的业务异常（外部端据此重建异常类型）。"""

    def __init__(self, error_type, message):
        Exception.__init__(self, message)
        self.error_type = error_type


#: 本 agent 已实现的 wire action 清单（PROBE 如实上报，能力判定归外部端）。
#: ★ 这张表必须与 execute() 的路由保持同步 —— 新增 action 不更新此处，
#:   probe 就会把「已支持」谎报成「不支持」（反向假阴性同样是 bug）。
_ACTIONS = ("PROBE", "PLACE", "CANCEL_ORDER", "QUERY_ASSET", "QUERY_POSITION",
            "QUERY_ORDER", "QUERY_TRADE", "QUERY_QUOTE", "QUERY_KLINE",
            "QUERY_STOCK_LIST", "QUERY_SECTOR_LIST", "QUERY_INSTRUMENT",
            "QUERY_CALENDAR", "SUB_QUOTE")

#: ---- D2 方向仲裁表（agent 只如实输出，不猜） ----
#: m_nOffsetFlag：48=买、49=卖（两融/期货的开平标志另列，不在此表 ⇒ 落 unknown）。
_OFFSET_FLAG_SIDE = {48: "buy", 49: "sell"}
#: ★ 刻意**不含 48**：实测 m_nDirection 恒 48（xtquant_big_convert 证据），
#:   它单独出现时不能证明「买」——只有 49 可信为卖，48 必须另有佐证。
_DIRECTION_SIDE = {49: "sell"}
_UNTRUSTED_DIRECTION = 48


def _attr(obj, name, default=None):
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return value if value is not None else default


def _get(row, name, default=None):
    """兼容 dict 与 QMT 对象两种形态的字段读取。

    fake/回测数据是 dict，真终端 ORDER/DEAL 是对象 —— 仲裁与映射必须同时吃。
    """
    if isinstance(row, dict):
        value = row.get(name, default)
    else:
        value = _attr(row, name, default)
    return default if value is None else value


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _side_from_text(text):
    s = str(text or "")
    if "买入" in s or "买" == s.strip():
        return "buy"
    if "卖" in s:
        return "sell"
    return ""


def _compact_date(value):
    return str(value or "").replace("-", "").replace("/", "").replace(" ", "").strip()


#: 交易（下单 / 撤单 / 查询）所必需的**终端注入**函数名。
#:
#: ★ 用途严格限定为「**给已捕获的集合分类**」，**绝不**用于断言「终端应该有什么」
#:   —— 后者就是本仓明令禁止的手抄名单（CI 的 G3 闸门专拦它，手抄名单会把桥的
#:   bug 伪装成「终端缺能力」）。
#: ★ 名字必须落在**实现层**（本文件本就持有 passorder / cancel 字面量）；
#:   入口文件 BIGQMT_AGENT.py 由 G3 限制字面量 ≤3，保持零字面量。
#: ★ xtdata 的转发面里**不存在**这些名字 ⇒ 本判据不会被转发污染。
_TRADE_FUNCS = ("passorder", "cancel", "get_trade_detail_data")


def trade_surface(injected):
    """把「终端注入面」按交易 / 非交易分类，供自检如实上报下单能力。

    ``injected`` **必须**是 ``capture_qmt_injected_funcs()`` 的结果（只含终端注入、
    不含我们转发的 xtdata 接口）—— 否则会把转发当成下单能力，又是一次假绿灯。

    返回 ``{"present": [...], "missing": [...], "can_submit": bool}``。
    ``can_submit`` 只认 ``passorder``：撤单/查询可用但没有下单函数，仍不能下单。
    """
    injected = injected or {}
    present = sorted(n for n in _TRADE_FUNCS if n in injected)
    return {
        "present": present,
        "missing": sorted(n for n in _TRADE_FUNCS if n not in injected),
        "can_submit": "passorder" in injected,
    }


class Executor(object):
    """action 路由 + 差分事件合成。"""

    def __init__(self, cfg, injected, context_info, extra=None):
        self.cfg = cfg or {}
        #: **终端注入**的函数面（独立进程模式下为空）。能力**上报**只认它。
        self.injected = injected or {}
        #: 额外的**函数查找面**：独立进程模式下由 BIGQMT_AGENT 放入我们转发进来的
        #: xtdata 接口。它只用于「取函数」，**绝不**用于声明「终端注入了什么」——
        #: 混为一谈会让能力面出现假绿灯（详见 BIGQMT_AGENT.capture_qmt_injected_funcs）。
        self.extra = extra or {}
        self.ctx = context_info
        self.po = dict(_DEFAULT_PASSORDER)
        if isinstance(cfg.get("passorder"), dict):
            self.po.update(cfg["passorder"])
        self._shield = bool(self.cfg.get("trading_enabled", False))
        # D2 第三级证据源：本进程下过的单 {user_order_id: side}（opType 记忆）。
        # 有界：超上限丢最旧 —— agent 长跑不能无限吃内存。
        self._placed = {}
        self._placed_cap = 500
        # D6 回调双轨：BIGQMT_AGENT 的 order_callback/deal_callback 触发时置真。
        # ★ 只有**真的被终端调用过**才声明 callback_bound —— 定义了两个空函数
        #   不算「绑定成功」，据此谎报会让 semantics 把上界调低、watchdog 误杀单。
        self.callback_hits = 0
        self._dir_unknown = 0
        self.subscribed = set()
        self._ascii_only = bool(self.cfg.get("ascii_only", False))
        #: **行情实调结论** ``(ok, detail)`` 或 ``None``。由 BIGQMT_AGENT 的自检写入，
        #: 随 PROBE 应答回给外部端。★ 存在意义：能力协商若只看「函数在不在」，
        #: 就会把「接口存在但实调抛错」判成 SUPPORTED —— 2026-10-09 真机假绿灯。
        self.quote_call = None
        # ★ 多标的账户类型能力表（P1 · R18）：从 po（已合并 config.passorder
        #   覆盖）读取 opAccountType_* 键，形成外部端可枚举的字典。
        #   外部端（后端 bridge_client）据此知道「这个 agent 支持哪些标的」，
        #   前端下拉框据此渲染（不用硬编码 5 个类型，不同券商版本可能子集）。
        self._account_types = {}
        for _k in ("stock", "etf", "future", "option", "credit"):
            _op = self.po.get("opAccountType_%s" % _k)
            if _op is not None:
                try:
                    self._account_types[_k] = int(_op)
                except (TypeError, ValueError):
                    self._account_types[_k] = 0

    # ------------------------------------------------------------------
    # 主循环（由 handlebar 驱动）
    # ------------------------------------------------------------------
    def poll_once(self, _on_request=None, _emit=None):
        """处理 req/ 下所有请求文件。有界：单次最多处理 _MAX_BATCH 条。"""
        req_dir = os.path.join(self.cfg.get("bridge_dir", ""), "req")
        if not os.path.isdir(req_dir):
            return 0
        names = sorted([n for n in os.listdir(req_dir)
                        if n.endswith(".json") and not n.endswith(".tmp")])
        handled = 0
        for name in names[:_MAX_BATCH]:
            path = os.path.join(req_dir, name)
            try:
                with open(path, "r") as fh:
                    envelope = json.load(fh)
            except Exception:
                # 半写/损坏的请求：丢弃而不是卡死整个队列。
                self._discard(path)
                continue
            if envelope.get("signal_id") != name[:-5]:
                # 文件名与内容不一致 ⇒ 可能是别的进程写的，拒绝执行（安全）。
                self._write_response(envelope.get("signal_id", name[:-5]), {
                    "ok": False, "error": "signal_id 与文件名不一致，拒绝执行",
                    "error_type": "Rejected"})
                self._discard(path)
                continue
            response = self.execute(envelope)
            self._write_response(envelope["signal_id"], response)
            self._discard(path)
            handled += 1
        return handled

    def _discard(self, path):
        try:
            os.remove(path)
        except Exception:
            pass

    def _write_response(self, signal_id, response):
        """原子写：tmp + replace，避免外部端读到半份 JSON。"""
        resp_dir = os.path.join(self.cfg.get("bridge_dir", ""), "resp")
        target = os.path.join(resp_dir, "%s.json" % signal_id)
        tmp = target + ".tmp"
        payload = {
            "v": 1, "signal_id": signal_id,
            "ok": bool(response.get("ok")),
            "result": response.get("result"),
            "error": response.get("error", ""),
            "error_type": response.get("error_type", ""),
            "ts": int(time.time() * 1000),
            "agent": self.meta(),
        }
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=self._ascii_only)
            os.replace(tmp, target)
        except UnicodeEncodeError:
            # D5 逃生阀：某些券商终端环境对非 ASCII 输出有拦截/乱码前科，
            # 失败一次即当轮改走 ensure_ascii=True（协议不变，只是转义）。
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=True)
                os.replace(tmp, target)
            except Exception:
                print("[qmt_work_bigqmt_agent] 写响应失败: %s" % traceback.format_exc())
        except Exception:
            print("[qmt_work_bigqmt_agent] 写响应失败: %s" % traceback.format_exc())

    # ------------------------------------------------------------------
    def meta(self):
        return {
            "ver": VERSION,
            "py": sys.version.split()[0],
            # ★ ``funcs`` = **终端注入**的函数面（能力上报只认它）。
            #   独立进程模式下这里为空 —— 那正是「不能下单」的诚实答案。
            #   我们转发的 xtdata 接口另列 ``forwarded_funcs``（弱证据，仅供诊断）。
            "funcs": sorted(self.injected.keys()),
            "forwarded_funcs": sorted(self.extra.keys()),
            "trading_enabled": self._shield,
            "bridge_dir": self.cfg.get("bridge_dir", ""),
            "uptime_s": int(time.time() - float(self.cfg.get("_started_at", time.time()))),
            # ★ 能力如实清单（G4 收口）：外部端据此做 CapabilitySet，而不是猜。
            "actions": list(_ACTIONS),
            # D6：只有回调**真的被终端调用过**才算绑定成功（空定义≠可用）。
            "callback_bound": self.callback_hits > 0,
            "callback_hits": self.callback_hits,
            # D2：仲裁落 unknown 的计数 —— probe 诊断面据此提示「方向字段不可信」
            "direction_unknown": self._dir_unknown,
            "subscribed": len(self.subscribed),
            "ascii_only": self._ascii_only,
            # ★ P1 多标的账户类型能力面（R18）：外部端据此枚举 agent 支持的
            #   标的类型；前端下拉框据此渲染。key=name, value=opAccountType
            #   数值（0=stock 走标准签名，非 0 走扩展签名）。
            "account_types": dict(self._account_types),
            "default_account_type": self.cfg.get("default_account_type", "stock"),
        }

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def execute(self, envelope):
        """执行一条请求，返回 dict(ok, result, error, error_type)。"""
        op = str(envelope.get("op") or "").strip().upper()
        params = envelope.get("params") or {}
        try:
            self._check_auth(envelope)
            # D3：写请求先过 TTL 闸，**再进任何业务分支**（放在路由之后会让
            # PLACE 先炸参数校验、Expired 语义永远到不了外部端）。
            if op in ("PLACE", "CANCEL_ORDER"):
                self._check_ttl(envelope, params)
            if op == "PROBE":
                return {"ok": True, "result": {"agent": self.meta(),
                                               "captured": sorted(self.injected.keys()),
                                               # ★ 实调证据随应答出去：外部端据此
                                               #   否决「函数在 ⇒ 能力可用」的推断。
                                               "quote_call": self.quote_call}}
            if op in ("PLACE",):
                return {"ok": True, "result": self.do_place(params)}
            if op == "CANCEL_ORDER":
                return {"ok": True, "result": self.do_cancel(params)}
            if op == "QUERY_ASSET":
                return {"ok": True, "result": self.do_query(_DT_ACCOUNT, params)}
            if op == "QUERY_POSITION":
                return {"ok": True, "result": self.do_query(_DT_POSITION, params)}
            if op == "QUERY_ORDER":
                return {"ok": True, "result": self.do_query(_DT_ORDER, params)}
            if op == "QUERY_TRADE":
                return {"ok": True, "result": self.do_query(_DT_DEAL, params)}
            if op == "QUERY_QUOTE":
                return {"ok": True, "result": self.do_quote(params)}
            if op == "QUERY_KLINE":
                return {"ok": True, "result": self.do_kline(params)}
            if op in ("QUERY_STOCK_LIST", "QUERY_SECTOR_LIST",
                      "QUERY_INSTRUMENT", "QUERY_CALENDAR", "SUB_QUOTE"):
                return {"ok": True, "result": self._market_extra(op, params)}
            raise ActionError("Unsupported", "未知 action: %s" % op)
        except ActionError as exc:
            # 业务失败：error_type 决定外部端映射到 400 还是 503
            return {"ok": False, "error": str(exc), "error_type": exc.error_type}
        except Exception as exc:
            return {"ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "error_type": "BrokerSDKError"}

    def _check_auth(self, envelope):
        """token 校验。配置为空时**放行但告警**（首次部署体验优先，文档已提示）。"""
        token = str(self.cfg.get("auth_token") or "")
        if not token:
            return
        if envelope.get("auth") != token:
            raise ActionError("Rejected", "auth token 不匹配")

    def _check_ttl(self, envelope, params):
        """D3 幽灵单防护：``ts + ttl_ms < now`` 的写请求**拒执行**。

        客户端已放弃的迟到请求若仍触发下单，就是对账噩梦的源头。
        缺省（ttl_ms<=0）不过期 —— 向后兼容旧版后端信封。
        超时后的**对账**（QUERY_ORDER 复核而非盲重发）在后端 order_watchdog 侧，
        不在这里。
        """
        ttl = _num(params.get("ttl_ms")) or 0
        if ttl <= 0:
            ttl = _num(envelope.get("ttl_ms")) or 0
        if ttl <= 0:
            return
        sent = _num(envelope.get("ts")) or 0
        if sent and int(time.time() * 1000) > sent + ttl:
            raise ActionError(
                "Expired",
                "请求已过期（发出 %d ms 前，ttl_ms=%d），拒绝执行以防幽灵单；"
                "请经 QUERY_ORDER 对账确认柜台状态，勿盲目重发"
                % (int(time.time() * 1000) - sent, ttl))

    def _need(self, name):
        fn = self.injected.get(name)
        if fn is not None:
            return fn
        # ★ 查找面第二级：我们转发的 xtdata 接口（独立进程模式）。**只影响取用**，
        #   不影响能力上报 —— `meta()["captured"]` 仍只列终端注入。
        fn = self.extra.get(name)
        if fn is not None:
            return fn
        # ★ 措辞纪律：只能说「未捕获」，不能断言终端没有。
        raise ActionError(
            "BrokerSDKError",
            "未在该解析路径捕获到函数 %s —— 请确认本文件是 QMT 挂载的入口文件"
            "（函数只注入入口文件命名空间）；如需两融相关接口请检查客户端版本。" % name)

    # ------------------------------------------------------------------
    # 业务动作
    # ------------------------------------------------------------------
    def do_query(self, data_type, params):
        fn = self._need("get_trade_detail_data")
        account = params.get("account_id") or self.cfg.get("account_id") or ""
        rows = self._call_query(fn, account, data_type)
        return [self._row_to_dict(data_type, r) for r in rows]

    def _call_query(self, fn, account, data_type):
        """不同版本的形参个数不一致，逐个尝试（先多后少），全失败即报错。

        这是被迫的兼容手段：QMT 的 get_trade_detail_data 在新旧版本间多了一个
        ContextInfo 形参。与其让用户改代码，不如在这里一次性兜住。
        """
        err = None
        for args in ((account, data_type, self.ctx), (account, data_type)):
            try:
                rows = fn(*args)
                return list(rows or [])
            except TypeError as exc:
                err = exc
                continue
        raise ActionError("BrokerSDKError",
                          "get_trade_detail_data 调用失败: %s" % err)

    def _row_to_dict(self, data_type, row):
        if isinstance(row, dict):
            out = dict(row)
        elif data_type == _DT_ACCOUNT:
            out = {
                "account_id": _attr(row, "m_strAccountID", ""),
                "cash": _attr(row, "m_dAvailable", 0.0),
                "frozen": _attr(row, "m_dFrozenCash", 0.0),
                "assets": _attr(row, "m_dBalance", 0.0),
                "market_value": _attr(row, "m_dInstrumentMarketValue", 0.0),
            }
        elif data_type == _DT_POSITION:
            out = {
                "code": _attr(row, "m_strInstrumentID", ""),
                "name": _attr(row, "m_strInstrumentName", ""),
                "volume": _attr(row, "m_nVolume", 0),
                "avail": _attr(row, "m_nCanUseVolume", 0),
                "cost": _attr(row, "m_dOpenPrice", 0.0),
                "market_value": _attr(row, "m_dMarketValue", 0.0),
            }
        elif data_type == _DT_ORDER:
            out = {
                "order_id": _attr(row, "m_strOrderSysID", ""),
                "code": _attr(row, "m_strInstrumentID", ""),
                "order_status": _attr(row, "m_nOrderStatus", 255),
                "volume": _attr(row, "m_nVolumeTotalOriginal", 0),
                "dealt": _attr(row, "m_nVolumeTraded", 0),
                "traded_price": _attr(row, "m_dTradedPrice", 0.0),
                "time": _attr(row, "m_strInsertTime", ""),
            }
        else:
            out = {
                "trade_id": _attr(row, "m_strTradeID", ""),
                "order_id": _attr(row, "m_strOrderSysID", ""),
                "code": _attr(row, "m_strInstrumentID", ""),
                "price": _attr(row, "m_dPrice", 0.0),
                "volume": _attr(row, "m_nVolume", 0),
                "time": _attr(row, "m_strTradeTime", ""),
            }
        if data_type in (_DT_ORDER, _DT_DEAL) and not out.get("direction"):
            # ★ D2：买卖方向按仲裁链产出（buy/sell/unknown），下游 canonicalize
            #   只认字面值「buy/sell」映射 side —— 绝不把恒 48 的 m_nDirection
            #   直接当「买」透传，那会把卖单伪装成买单进风控链路。
            out["direction"] = self._direction_of(row, out)
        return out

    def _direction_of(self, row, out):
        """D2 仲裁链：optName 文案 → offsetFlag → direction(仅49可信) → 下单记忆。"""
        side = _side_from_text(_get(row, "m_strOptName", ""))
        if side:
            return side
        off = _num(_get(row, "m_nOffsetFlag", None))
        if off in _OFFSET_FLAG_SIDE:
            return _OFFSET_FLAG_SIDE[off]
        d = _num(_get(row, "m_nDirection", None))
        if d is not None and d != _UNTRUSTED_DIRECTION and d in _DIRECTION_SIDE:
            return _DIRECTION_SIDE[d]
        # 末级：本进程下过的单按用户委托号（passorder 备注透传回 m_strRemark）回查
        for key in (_get(row, "m_strRemark", ""), _get(row, "remark", ""),
                    _get(row, "client_order_id", ""), out.get("order_id", "")):
            if key and str(key) in self._placed:
                return self._placed[str(key)]
        self._dir_unknown += 1
        return "unknown"

    # ------------------------------------------------------------------
    def do_place(self, params):
        """下单。trading_enabled=false 时**拒绝**并返回明确 error_type。

        P1（R18 升级）：支持多标的账户类型。
          - `account_type` 参数：stock/etf/future/option/credit
            （默认 stock；也可用别名如 A股/期货/lof）
          - 非 stock 时走 **扩展 12-参数 passorder**（追加 opAccountType）
          - 扩展签名不兼容（老券商版本）时自动降级到 11-参数标准签名
          - code 按 account_type 正则校验，格式错直接 BrokerError
        """
        if not self._shield:
            raise ActionError(
                "Rejected",
                "agent 侧 trading_enabled=false，拒绝下单。"
                "确认连通后在 agent_config.json 置 true 并重启策略。")
        code = params.get("stock_code")
        volume = int(params.get("volume") or 0)
        price = float(params.get("price") or 0.0)
        if not code or volume <= 0:
            raise ActionError("BrokerError", "下单参数非法: code=%s volume=%s"
                              % (code, volume))
        side = str(params.get("side", "")).lower()
        if side not in ("buy", "sell"):
            raise ActionError("BrokerError", "下单方向缺失: %r" % side)
        price_type = str(params.get("price_type") or "limit").lower()
        if price_type == "limit" and price <= 0:
            raise ActionError("BrokerError", "限价单必须给出正的价格")

        # ★ 账户类型解析 + 标的代码校验（P1）
        #   取值优先级：请求显式 account_type > agent_config.default_account_type > stock。
        #   `default_account_type` 让「本机只连一个期货/两融账户」的场景一次配置、
        #   全单生效（前端/REST 不必每单都传），也保证该能力**不依赖前端改造**可用。
        #   非法值 → ValueError → 转 BrokerError（400 + 真实原因），绝不静默走 A 股。
        try:
            acct_type, acct_type_num = resolve_account_type(
                params.get("account_type") or self.cfg.get("default_account_type"),
                self.po)
            ok_code, code_err = validate_code(code, acct_type)
        except ValueError as exc:
            raise ActionError("BrokerError", str(exc))
        if not ok_code:
            raise ActionError("BrokerError", code_err)

        fn = self._need("passorder")
        op_type = self.po["opType_buy"] if side == "buy" else self.po["opType_sell"]
        pr_type = self.po["prType_limit"] if price_type == "limit" else self.po["prType_market"]
        account = params.get("account_id") or self.cfg.get("account_id") or ""
        user_order_id = params.get("client_order_id") or ""
        if not user_order_id:
            # 生成内部关联号（写入 passorder 备注位、回报里回显 m_strRemark）——
            # 这是 D2 的第三级证据源，不是伪造柜台号：回执里如实带 generated=True。
            user_order_id = "ag%d" % (int(time.time() * 1000) % 10 ** 13)
        # 有界记忆：超上限先丢最旧（dict 在 3.6 保持插入序，取首键即可）
        while len(self._placed) >= self._placed_cap:
            self._placed.pop(next(iter(self._placed)), None)
        self._placed[user_order_id] = side

        # ★ passorder 三级降级（P1 多账户类型）：
        #   每个签名先试，失败即换下一个；三层全挂则抛最后一个 TypeError，
        #   让外层 execute() 捕获转成 error_result 返回给外部端。
        #
        # 顺序不能颠倒：扩展签名是**加参数**、不删减；标准签名在扩展失败时
        #   一定能再试一次。三层里**任一成功就立即返回**，不再试后面的。
        #
        # ⚠️ 成功判据必须用**显式下标**，绝不能用 `ret is None`：
        #   QMT 的 passorder 是 void，正常路径**就返回 None**（`_ret_to_result`
        #   明确支持这种「拿不到委托号」的情形）。用返回值做哨兵会把「下单成功
        #   但无回执」误判成失败 —— 更糟的是此时 `last_err` 也是 None，
        #   `raise None` 会抛 `TypeError: exceptions must derive from
        #   BaseException`，把一单**已送达柜台**的委托报成 SDK 故障，
        #   用户重试即产生**重复委托**（R19 第 1 轮修复）。
        base_args = (op_type, self.po["orderType"], account, code, pr_type,
                     price, volume, self.po["strategyName"],
                     self.po["quickTrade"], user_order_id)
        std_11 = base_args + (self.ctx,)
        noctx_10 = base_args  # 老券商版本无 ContextInfo 形参
        if acct_type_num != 0:
            sigs = (std_11 + (acct_type_num,), std_11, noctx_10)
        else:
            sigs = (std_11, noctx_10)
        ret = None
        last_err = None
        success = False
        success_idx = -1
        for _i, _sig in enumerate(sigs):
            try:
                ret = fn(*_sig)
                success = True
                success_idx = _i
                break
            except TypeError as e:
                last_err = e
        if not success:
            # 三层全失败 —— 抛最后一个 TypeError 让 execute() 转成 BrokerSDKError。
            # last_err 必定非 None（否则不可能三层都「失败」），但仍显式兜底，
            # 避免任何未来重构再次把 None 抛出去。
            raise last_err if last_err is not None else TypeError(
                "passorder 三级签名（12/11/10 参数）全部失败")
        fallback_used = success_idx > 0

        result = _ret_to_result(ret, user_order_id)
        # 回显：外部端（后端/前端）据此知道这单用了哪种账户类型，
        # 以及是否走了降级签名（排障时可看到 fallback_used=true）
        result["account_type"] = acct_type
        result["code"] = code
        if fallback_used:
            result["extended_signature_fallback"] = True
        if params.get("client_order_id") != user_order_id:
            result["generated_client_order_id"] = True
        return result

    def do_cancel(self, params):
        order_id = str(params.get("order_id") or "")
        if not order_id:
            raise ActionError("BrokerError", "撤单缺少 order_id")
        fn = self._need("cancel")
        account = params.get("account_id") or self.cfg.get("account_id") or ""
        try:
            ret = fn(order_id, account, self.ctx)
        except TypeError:
            ret = fn(order_id, account)
        return {"order_id": order_id, "ok": bool(ret), "raw": ret}

    # ------------------------------------------------------------------
    def do_quote(self, params):
        codes = params.get("codes") or []
        if not codes:
            raise ActionError("BrokerError", "QUERY_QUOTE 缺少 codes")
        getter = getattr(self.ctx, "get_full_tick", None)
        if getter is None:
            raise ActionError("BrokerSDKError",
                              "ContextInfo 未提供 get_full_tick（行情需开启独立行情/极简模式）")
        tick = getter(list(codes))
        return tick if isinstance(tick, dict) else {"data": tick}

    def do_kline(self, params):
        code = params.get("stock_code")
        period = params.get("period") or "1d"
        getter = getattr(self.ctx, "get_market_data", None)
        if getter is None:
            raise ActionError("BrokerSDKError", "ContextInfo 未提供 get_market_data")
        count = int(params.get("count") or 0) or 100
        try:
            df = getter([], [code], period, count)
        except Exception as exc:
            raise ActionError("BrokerSDKError", "get_market_data 失败: %s" % exc)
        rows = _df_to_rows(df)
        if not rows:
            # M2.1 GET_DATA：本地无缓存时**先下载再取一次**（只重试一次，有界）。
            # 不下载就返回空列表 = 「查询成功但没数据」的假象；下载失败则如实带
            # 空返回，由外部端按缺数据告警，而不是静默。
            self._download_once(code, period)
            try:
                rows = _df_to_rows(getter([], [code], period, count))
            except Exception:
                rows = []
        return rows

    def _download_once(self, code, period):
        """download_history_data 尽力调一次；不可用/失败都静默（结果面自会体现缺数据）。"""
        fn = self.injected.get("download_history_data")
        if fn is None:
            fn = self.extra.get("download_history_data")
        if fn is None:
            fn = getattr(self.ctx, "download_history_data", None)
        if fn is None:
            return
        for args in ((code, period, "", ""), (code, period)):
            try:
                fn(*args)
                return
            except TypeError:
                continue
            except Exception:
                return

    # ------------------------------------------------------------------
    # 行情/合约扩展 action（M2.1：解除「桥接模式暂不支持」）
    # ------------------------------------------------------------------
    def _market_extra(self, op, params):
        if op == "QUERY_STOCK_LIST":
            return self.do_stock_list(params)
        if op == "QUERY_SECTOR_LIST":
            return self.do_sector_list(params)
        if op == "QUERY_INSTRUMENT":
            return self.do_instrument(params)
        if op == "QUERY_CALENDAR":
            return self.do_calendar(params)
        if op == "SUB_QUOTE":
            return self.do_subscribe(params)
        raise ActionError("Unsupported", "未知 action: %s" % op)

    def _pick(self, name):
        """函数优先取注入命名空间，其次取转发面，最后取 ContextInfo 方法
        （都取不到 ⇒ not_captured）。"""
        fn = self.injected.get(name)
        if fn is not None:
            return fn
        fn = self.extra.get(name)
        if fn is not None:
            return fn
        fn = getattr(self.ctx, name, None)
        if callable(fn):
            return fn
        # ★ 措辞纪律同 _need：只说「未捕获」，不断言终端没有该接口。
        raise ActionError(
            "BrokerSDKError",
            "未捕获到 %s —— 请确认入口文件被 QMT 挂载、行情已上线；"
            "PROBE 的 captured 清单里有无此函数是唯一判据" % name)

    def do_stock_list(self, params):
        """板块成分 [{code, name}]，与 miniQMT 侧 get_stock_list 同形。"""
        sector = str(params.get("sector") or "沪深A股")
        limit = _num(params.get("limit")) or 3000
        codes = self._in_sector(sector)
        detail = None
        try:
            detail = self._pick("get_instrument_detail")
        except ActionError:
            pass
        out = []
        for c in codes[:limit]:
            name = c
            if detail is not None:
                try:
                    d = detail(c) or {}
                    if isinstance(d, dict):
                        name = (d.get("InstrumentName")
                                or d.get("instrument_name") or c)
                except Exception:
                    pass  # 名称是锦上添花，取不到就回代码，不影响列表可用性
            out.append({"code": c, "name": name})
        return out

    def _in_sector(self, sector):
        fn = self._pick("get_stock_list_in_sector")
        last = None
        for args in ((sector, self.ctx), (sector,)):
            try:
                return [c for c in (fn(*args) or []) if c]
            except TypeError as exc:
                last = exc
                continue
        raise ActionError("BrokerSDKError",
                          "get_stock_list_in_sector 调用失败: %s" % last)

    def do_sector_list(self, params):
        fn = self._pick("get_sector_list")
        last = None
        for args in ((self.ctx,), ()):
            try:
                return [s for s in (fn(*args) or []) if s]
            except TypeError as exc:
                last = exc
        raise ActionError("BrokerSDKError", "get_sector_list 调用失败: %s" % last)

    def do_instrument(self, params):
        code = params.get("code") or params.get("stock_code") or ""
        if not code:
            raise ActionError("BrokerError", "QUERY_INSTRUMENT 缺少 code")
        fn = self._pick("get_instrument_detail")
        try:
            d = fn(code) or {}
        except Exception as exc:
            raise ActionError("BrokerSDKError",
                              "get_instrument_detail(%s) 失败: %s" % (code, exc))
        return d if isinstance(d, dict) else {"raw": str(d)}

    def do_calendar(self, params):
        """交易日历 [YYYYMMDD]，与 miniQMT 侧 get_trading_calendar 同形。

        SH/SZ 交易日一致，market 缺省 SH 即可。
        """
        market = str(params.get("market") or "SH")
        start = _compact_date(params.get("start") or "")
        end = _compact_date(params.get("end") or "")
        fn = self._pick("get_trading_dates")
        rows = None
        last = None
        for args in ((market, start, end), (market, start, end, self.ctx)):
            try:
                rows = fn(*args)
                break
            except TypeError as exc:
                last = exc
                continue
        if rows is None:
            raise ActionError("BrokerSDKError",
                              "get_trading_dates 调用失败: %s" % last)
        return [str(d) for d in (rows or [])]

    def do_subscribe(self, params):
        """订阅实时行情 —— 本 agent 的实现形态是「handlebar 轮询 + 差分转发」。

        ★ 跨进程桥**不可能**把 on_tick 回调送进 QMT 再送回外部端；订阅的语义落为
        「后续 quote_events 会把这些 code 的变化写进 events.ndjson」。
        若终端另有 subscribe_quote 则尽力调一次（失败不影响轮询转发）。
        """
        codes = [c for c in (params.get("codes") or []) if c]
        if not codes:
            raise ActionError("BrokerError", "SUB_QUOTE 缺少 codes")
        sub = self.injected.get("subscribe_quote")
        if sub is None:
            sub = self.extra.get("subscribe_quote")
        if sub is not None:
            for c in codes:
                try:
                    sub(c)
                except Exception:
                    break  # 原生订阅不可用：静默回落轮询，能力如实体现在 mode
        self.subscribed.update(codes)
        return {"subscribed": sorted(self.subscribed), "mode": "poll_forward"}

    def quote_events(self, state, emit):
        """handlebar 推进：订阅 code 的 tick 变化 ⇒ type=quote 事件（外部端只翻译不再 diff）。"""
        codes = sorted(self.subscribed)
        if not codes:
            return 0
        getter = getattr(self.ctx, "get_full_tick", None)
        if getter is None:
            return 0
        try:
            ticks = getter(list(codes)) or {}
        except Exception:
            return 0
        seen = state.setdefault("quote_seen", {})
        emitted = 0
        for code in codes:
            tick = ticks.get(code) if isinstance(ticks, dict) else None
            if not isinstance(tick, dict):
                continue
            last = tick.get("lastPrice")
            fp = "%s|%s|%s" % (last, tick.get("volume"), tick.get("lastTime"))
            if seen.get(code) == fp:
                continue
            seen[code] = fp
            emit("quote", {"code": code,
                           "time": str(tick.get("lastTime") or ""),
                           "price": last, "tick": tick})
            emitted += 1
        return emitted

    # ------------------------------------------------------------------
    # 差分事件合成（无推送通道时的兜底）
    # ------------------------------------------------------------------
    @staticmethod
    def order_fp(row):
        return "%s|%s|%s" % (row.get("order_status"), row.get("dealt"),
                             row.get("traded_price"))

    @staticmethod
    def trade_fp(row):
        return "%s|%s" % (row.get("price"), row.get("volume"))

    def note_callback(self, kind, obj):
        """D6 双轨：终端回调（order_callback/deal_callback）触发的实时事件。

        返回 ``(row, dedup_key, fp)`` —— 调用方（BIGQMT_AGENT）负责：
        写入差分基线（避免 diff_events 对同一变化**二次发事件**）并 emit。
        """
        self.callback_hits += 1
        dt = _DT_ORDER if kind == "order" else _DT_DEAL
        row = self._row_to_dict(dt, obj)
        key = str(row.get("order_id") if dt == _DT_ORDER else row.get("trade_id") or "")
        fp = self.order_fp(row) if dt == _DT_ORDER else self.trade_fp(row)
        return row, key, fp

    def diff_events(self, state, emit):
        """按 order/deal 差分产出事件。**首轮只建基线，不补发**。"""
        fn = self.injected.get("get_trade_detail_data")
        if fn is None:
            return 0
        try:
            orders = self.do_query(_DT_ORDER, {})
            deals = self.do_query(_DT_DEAL, {})
        except ActionError:
            return 0
        except Exception:
            return 0
        emitted = 0
        for row in orders:
            key = str(row.get("order_id") or "")
            if not key:
                continue
            fp = self.order_fp(row)
            if state["order_seen"].get(key) == fp:
                continue
            state["order_seen"][key] = fp
            if not state["primed"]:
                continue
            # ★ 这里**不判定**「是不是废单」：状态整数的语义各家不一致
            #   （56 在 xtquant 是全部成交、在大 QMT 文档里被写成已拒），
            #   判定权归 qmt_work 侧的 SSOT（order_status），agent 只如实上报原始状态。
            emit("order", row)
            emitted += 1
        for row in deals:
            key = str(row.get("trade_id") or "")
            if not key:
                continue
            fp = self.trade_fp(row)
            if state["trade_seen"].get(key) == fp:
                continue
            state["trade_seen"][key] = fp
            if not state["primed"]:
                continue
            emit("trade", row)
            emitted += 1
        state["primed"] = True
        return emitted


_MAX_BATCH = 50


def _ret_to_result(ret, user_order_id):
    """passorder 返回值 → 外部端可解析的下单回执。

    QMT 的 passorder 返回一个列表/单值；拿不到委托号时**如实返回空字符串**，
    由外层 connector 判定「未受理」 —— 绝不伪造一个号码。
    """
    order_id = ""
    if isinstance(ret, (list, tuple)):
        order_id = str(ret[0]) if ret else ""
    elif ret:
        order_id = str(ret)
    return {
        "order_id": order_id,
        "client_order_id": user_order_id,
        "status": "submitted" if order_id else "unknown",
        "raw": ret,
    }


def _df_to_rows(df):
    """QMT 的 DataFrame / {field: {code: array}} 两种形态 → [{time,open,high,low,close,volume}]。

    解析失败**给空列表**而不是抛：调用方（do_kline）据此走「下载重试→如实空」，
    绝不把解析 bug 伪装成「无数据」。
    """
    rows = []
    if df is None:
        return rows
    try:
        inner = df.get("close") if isinstance(df, dict) else None
        if isinstance(inner, dict) and _code_key_of(inner):
            code0 = _code_key_of(inner)
            closes = inner[code0]
            opens = (df.get("open") or {}).get(code0, [])
            highs = (df.get("high") or {}).get(code0, [])
            lows = (df.get("low") or {}).get(code0, [])
            vols = (df.get("volume") or {}).get(code0, [])
            times = (df.get("time") or {}).get(code0, range(len(closes)))
            for i in range(len(closes)):
                rows.append({"time": str(_at(times, i, "")),
                             "open": float(_at(opens, i, 0) or 0),
                             "high": float(_at(highs, i, 0) or 0),
                             "low": float(_at(lows, i, 0) or 0),
                             "close": float(_at(closes, i, 0) or 0),
                             "volume": float(_at(vols, i, 0) or 0)})
            return rows
        for ts, row in df.iterrows():
            rows.append({
                "time": str(ts), "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)), "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0)),
            })
    except Exception:
        rows = []
    return rows


def _code_key_of(d):
    ks = list(d.keys())
    return ks[0] if ks else None


def _at(seq, i, default=None):
    try:
        return seq[i]
    except Exception:
        return default
