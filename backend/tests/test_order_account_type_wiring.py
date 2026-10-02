# -*- coding: utf-8 -*-
"""下单级 account_type **全链路贯通**回归锁（R19 第 1 轮补）。

背景（真实发现的断链）
--------------------
R18 给大 QMT agent 的 ``do_place`` 加了 ``account_type``（→ ``opAccountType``
→ 扩展 12-arg passorder 签名），但**没有任何生产者**：

* ``connectors/ports.py::OrderRequest`` 没有该字段；
* ``connectors/generic.py::place_order`` 的 payload 不含它；
* ``connectors/dialects/bigqmt_v1.py::prepare`` 白名单式产出把它丢掉；
* ``gateway/execution.py::_make_order_request`` 也不传；
* REST ``/trade/order`` / ``/signal/submit`` 的 body 没人读。

于是 ``do_place(params.get("account_type"))`` 永远是 None → 能力**永不可达**，
探针 ``meta()["account_types"]`` 是「假绿灯」：前端看得到、下单用不上。

本文件把每一环都钉死，避免再次退化成孤儿逻辑。链路：
    REST body → Signal → SignalRouter.submit → ExecutionService.place_order
    → _make_order_request → OrderRequest → GenericConnector payload
    → BigQmtV1.prepare → wire params → agent Executor.do_place
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connectors.dialects.bigqmt_v1 import BigQmtV1  # noqa: E402
from connectors.dialects.base import Ops  # noqa: E402
from connectors.ports import InstrumentId, OrderRequest  # noqa: E402


def test_order_request_has_account_type_default_empty():
    """canonical DTO 必须携带该字段，默认空串（= 不覆盖，向后兼容）。"""
    req = OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                       side="buy", quantity=100, price=35.0)
    assert req.account_type == ""
    req.validate()


def test_order_request_account_type_roundtrip():
    req = OrderRequest(instrument=InstrumentId(code="IF2312", exchange="SHF"),
                       side="buy", order_type="market", quantity=1,
                       account_type="future")
    assert req.account_type == "future"


def test_dialect_prepare_emits_account_type():
    """方言是 wire 的唯一出口：必须把它写进 params，否则 agent 永远看不到。"""
    d = BigQmtV1()
    params = d.prepare(Ops.PLACE_ORDER, {
        "code": "IF2312.SHF", "side": "buy", "order_type": "limit",
        "price": 3800.0, "quantity": 1, "account_type": "future",
    })
    assert params["account_type"] == "future"
    assert params["stock_code"] == "IF2312.SHF"


def test_dialect_prepare_defaults_account_type_empty():
    """未指定时产出空串（不是丢键）：agent 侧据此走 default_account_type。"""
    d = BigQmtV1()
    params = d.prepare(Ops.PLACE_ORDER, {
        "code": "600036.SH", "side": "buy", "order_type": "limit",
        "price": 35.0, "quantity": 100,
    })
    assert params["account_type"] == ""


def test_dialect_prepare_still_drops_unknown_keys():
    """白名单纪律不被破坏：未知键仍必须被丢弃（easytrader #520 教训）。"""
    d = BigQmtV1()
    params = d.prepare(Ops.PLACE_ORDER, {
        "code": "600036.SH", "side": "buy", "order_type": "limit",
        "price": 35.0, "quantity": 100, "evil_key": "boom",
    })
    assert "evil_key" not in params


def test_generic_connector_payload_includes_account_type():
    """连接器构造 payload 时必须带上，否则方言拿不到。"""
    import connectors.generic as generic

    src = inspect.getsource(generic.GenericConnector.place_order)
    assert '"account_type": request.account_type' in src


@pytest.mark.parametrize("path,needle", [
    ("gateway/execution.py", "account_type"),
    ("connectors/bigqmt_bridge.py", "account_type"),
    ("connectors/bigqmt_gateway.py", "account_type"),
    ("gateway/signal_router.py", "account_type"),
    ("app/routes/trade.py", "account_type"),
    ("app/routes/signal.py", "account_type"),
])
def test_chain_sources_mention_account_type(path, needle):
    """全链路每一环的源码都必须出现该参数（结构性防断链）。

    刻意用源码扫描而不是逐层调用：这些层跨越 async/子进程边界，逐个真跑成本高，
    而**断链的形态**恰恰是「某一环忘了写」——文本层面就能抓住。
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    full = os.path.normpath(os.path.join(root, path))
    src = open(full, encoding="utf-8").read()
    assert needle in src, f"{path} 未透传 {needle}（链路在此断开）"


def test_execution_place_order_signature_accepts_account_type():
    from gateway.execution import ExecutionService

    sig = inspect.signature(ExecutionService.place_order)
    assert "account_type" in sig.parameters


def test_signal_router_submit_signature_accepts_account_type():
    from gateway.signal_router import Signal, SignalRouter

    assert "account_type" in inspect.signature(SignalRouter.submit).parameters
    assert Signal(source="s", code="600036.SH", side="buy", volume=1).account_type == ""


def test_agent_place_reads_account_type_or_config_default():
    """agent 端既认请求参数，也认 agent_config.default_account_type。"""
    agent_dir = os.path.join(os.path.dirname(__file__), "..", "agent_bigqmt")
    src = open(os.path.join(agent_dir, "qmt_api.py"), encoding="utf-8").read()
    assert 'params.get("account_type") or self.cfg.get("default_account_type")' in src


# ---------------------------------------------------------------------------
# ★ 反向护栏：mini 适配器**不得**收到 account_type（否则下单必 TypeError）
# ---------------------------------------------------------------------------
def test_mini_adapter_is_never_handed_account_type():
    """``QMT_USE_PORTS=0``（默认）下 execution 直调 ``bridge.gateway.place_order``。

    mini 网关底层是 ``trader.order_stock``，**没有** ``opAccountType`` 形参
    （信用语义由连接级 account_type 派生）。若无条件追加第 8 个位置实参，
    mini 连接下单会 TypeError。本用例锁死「按签名投递」。
    """
    from gateway.execution import _account_type_arg
    from xtquant_client.xtp.adapter import XTPQuantAdapter

    class _MiniGateway(XTPQuantAdapter):  # 只借签名，不实例化真实 SDK
        pass

    class _FakeMini:
        place_order = XTPQuantAdapter.place_order

    class _FakeBig:
        def place_order(self, code, direction, price_type, price, volume,
                        strategy_name="", remark="", account_type=""):
            return {}

    assert _account_type_arg(_FakeMini(), "future") == ()
    assert _account_type_arg(_FakeBig(), "future") == ("future",)
    assert _account_type_arg(_FakeBig(), "") == ()
    # 缓存生效（同一函数对象第二次不再 inspect）
    assert _account_type_arg(_FakeBig(), "etf") == ("etf",)


def test_bigqmt_gateway_declares_account_type():
    from connectors.bigqmt_gateway import _BigQmtGateway

    import inspect as _i
    assert "account_type" in _i.signature(_BigQmtGateway.place_order).parameters


def test_mini_abc_place_order_has_no_account_type():
    """mini 的 ABC 契约也不应带它（避免「收下却不用」的无操作参数）。"""
    import inspect as _i

    from xtquant_client.gateway import XTQuantGateway

    assert "account_type" not in _i.signature(XTQuantGateway.place_order).parameters


# ---------------------------------------------------------------------------
# 跨语言对账：前端下拉选项 ⊆ 后端 canonical 类型（防手抄别名表漂移）
# ---------------------------------------------------------------------------
def test_frontend_account_type_options_match_agent_canonical_keys():
    """前端 `ACCOUNT_TYPE_OPTIONS` 的值必须与 agent 的 canonical key 完全一致。

    为什么跨语言也要锁：前端是一份**手抄**的选项表。后端将来新增/重命名一个
    canonical 类型（比如加 ``bond``），前端不跟就会 400 —— 而两端各自的单测
    都还是绿的（**单侧绿，最难发现**）。
    """
    import re

    root = os.path.join(os.path.dirname(__file__), "..", "..")
    tsx = os.path.normpath(os.path.join(
        root, "frontend-next", "src", "domains", "trading", "OrderForm.tsx"))
    src = open(tsx, encoding="utf-8").read()
    block = src.split("export const ACCOUNT_TYPE_OPTIONS", 1)
    assert len(block) == 2, "OrderForm.tsx 里找不到 ACCOUNT_TYPE_OPTIONS 导出"
    body = block[1].split("];", 1)[0]
    fe_values = set(re.findall(r'value:\s*"([a-z_]*)"', body))
    fe_values.discard("")

    canonical = _agent_canonical_account_types()
    assert fe_values == canonical, (
        f"前端账户类型选项 {sorted(fe_values)} 与后端 canonical {sorted(canonical)} 不一致"
        "（改一侧必须同步另一侧）")


def _agent_canonical_account_types() -> set[str]:
    """从 agent 源码 AST 里取 `_ACCOUNT_TYPE_ALIASES` 的**值**集合。

    刻意不 import：`agent_bigqmt` 是 py3.6 bundle 源码，不在 backend 的 import
    根上（各测试文件各自 sys.path.insert）。AST 读取无副作用、也不受包布局影响。
    """
    import ast

    agent_dir = os.path.join(os.path.dirname(__file__), "..", "agent_bigqmt")
    src = open(os.path.join(agent_dir, "qmt_api.py"), encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_ACCOUNT_TYPE_ALIASES" for t in node.targets):
            return {v.value for v in node.value.values}
    raise AssertionError("qmt_api.py 里找不到 _ACCOUNT_TYPE_ALIASES")


def test_frontend_order_payload_type_declares_account_type():
    """前端下单载荷类型必须声明该字段（否则界面选了也发不出去）。"""
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    ts = os.path.normpath(os.path.join(
        root, "frontend-next", "src", "services", "api", "trade.ts"))
    src = open(ts, encoding="utf-8").read()
    block = src.split("export interface SubmitOrderPayload", 1)
    assert len(block) == 2
    assert "account_type?: OrderAccountType" in block[1].split("}", 1)[0]


def test_submit_test_doubles_cover_the_real_router_signature():
    """所有 ``SignalRouter.submit`` 的测试替身必须覆盖真签名（TD-36 真回归）。

    R18 给 submit 加 ``account_type`` 时漏改 ``test_p0_manual_confirm.py`` 的
    ``_SpyRouter``，路由一传该参数即 ``TypeError``（3 例失败，且只有**全量测试**才暴露）。
    与 TD-34「给 ``sync_many`` 加参数打断所有替身」同一族 —— 用**真签名**做真源，
    替身缺参直接点名，避免再次「接口加参 → 替身静默落后 → 运行时才炸」。
    """
    import ast
    from pathlib import Path

    from gateway.signal_router import SignalRouter

    real = set(inspect.signature(SignalRouter.submit).parameters) - {"self"}
    tests_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name != "submit":
                continue
            args = node.args
            params = {p.arg for p in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            params.discard("self")
            if args.kwarg is not None:      # **kwargs 天然见容一切
                continue
            if "code" not in params:        # 非 SignalRouter 替身（如 scheduler.submit(spec)）
                continue
            missing = sorted(real - params)
            if missing:
                offenders.append(f"{path.name}: submit 缺参 {missing}")
    assert not offenders, (
        "替身签名落后于真实 SignalRouter.submit：\n" + "\n".join(offenders))
