"""大 QMT 桥的**接口面对齐**门禁（可证伪）。

为什么需要这个文件
------------------
``BrokerManager.Connection`` 有两个槽位，二者的调用形态不同：

* ``bridge``  —— ``await bridge.start()`` / ``await bridge.stop()`` / ``bridge.call``
  / ``bridge.pump_running()`` / ``bridge.start_pump_on(loop)``（小 QMT 侧由
  ``XTQuantBridge`` 提供）；
* ``adapter`` —— 同步属性 + ``await bridge.call(conn.adapter.X)``（小 QMT 侧由真
  适配器提供）。

大 QMT 连接只有**一个对象**同时占两个槽位，因此它必须**同时满足两套契约**。
历史上这里只实现了一部分，造成四条真实断链（不是理论风险）：

1. ``await conn.bridge.start()`` 拿到 ``None`` → ``TypeError``，连接在启动阶段
   永远被判失败；
2. ``conn.bridge.stop()`` 不存在 → 优雅停机 AttributeError；
3. ``start_pump_on`` 缺失 + ``pump_running()`` 返回 ``_connected`` → 事件泵
   **从未启动**，agent 写出的委托/成交/行情事件永远到不了前端；
4. ``conn.adapter.get_account()`` 等直接 AttributeError → 账户看板对桥连接全空。

本文件的判据刻意做成**扫描式**而不是手抄清单：直接扫产线代码里出现的
``gateway.<name>`` / ``conn.adapter.<name>``，要求桥的对应面必须实现。
手抄清单只能证明「我当时想到了这些」；扫描能证明「**今天**代码里用到的每一个
都在」。新增调用点而桥没跟上 ⇒ 本测试立刻红。
"""
import ast
import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# gateway 面（_BigQmtGateway）在独立模块里 —— 2026-10-01 按职责从 bigqmt_bridge 拆出
# （后者一度越过 50KB 单文件上限）。两个面仍由本文件一起对账，因为它们是**同一个
# Connection 对象的两套契约**，分开测就失去了「两套面互不矛盾」的意义。
from connectors.bigqmt_bridge import BigQmtBridge  # noqa: E402
from connectors.bigqmt_gateway import _BigQmtGateway  # noqa: E402
from xtquant_client.gateway import XTQuantBridge, XTQuantGateway  # noqa: E402

#: 扫描产线代码时排除的目录（第三方/构建产物/测试自身）。
_SKIP_DIRS = {".venv", "dist", "runtimes", "build", "__pycache__", "logs", "data",
              "output", "static", "node_modules", "tests"}


#: ``XTQuantBridge`` 自己的实现文件：里面 ``self.gateway`` 指的是
#: ``XTQuantGateway``（真适配器），与「连接对象的 bridge.gateway」不是同一个东西，
#: 混进来会误报（实测报出 ``gateway.close`` / ``gateway.start``）。
_SELF_BRIDGE_FILE = (BACKEND / "xtquant_client" / "gateway.py").resolve()


def _prod_sources(skip_self_bridge: bool = False):
    """产出产线 .py 文件路径（排除第三方/构建产物/测试）。"""
    for path in BACKEND.rglob("*.py"):
        parts = set(path.relative_to(BACKEND).parts)
        if parts & _SKIP_DIRS:
            continue
        if skip_self_bridge and path.resolve() == _SELF_BRIDGE_FILE:
            continue
        yield path


def _attr_names_on(obj_attr: str, skip_self_bridge: bool = False) -> set[str]:
    """产线代码里 ``<something>.<obj_attr>.<name>`` 形式的 ``name`` 集合。

    ★ 用 **AST** 而不是正则：正则会把 ``logging.getLogger("qmt_work.gateway.execution")``
      这类**字符串字面量**也算成调用点（实测误报出 ``gateway.execution`` /
      ``gateway.close``），让门禁变成噪声。AST 只看真实属性访问，不会误报。
    """
    out: set[str] = set()
    for path in _prod_sources(skip_self_bridge):
        try:
            tree = ast.parse(path.read_text("utf-8", errors="replace"))
        except (OSError, SyntaxError):  # pragma: no cover -  IO 竞态 / 非 utf8
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            parent = node.value
            if isinstance(parent, ast.Attribute) and parent.attr == obj_attr:
                out.add(node.attr)
    return out


# ---------------------------------------------------------------------------
# 1) bridge 槽位：与 XTQuantBridge 同形
# ---------------------------------------------------------------------------

#: ``BrokerManager`` 及其守护任务实际依赖的 bridge 方法（形态敏感，单列）。
#: ★ 这里**不能**把 ``close`` 算进来：``XTQuantBridge`` 的关闭口叫 ``stop``，
#:   ``close`` 是 **adapter** 槽位的方法（``BrokerManager.disconnect`` 调的
#:   ``conn.adapter.close()``）。混在一起会把「两个槽位各有各的关闭口」这条
#:   真实约束抹平。
#: 注意 ``is_connected`` **不在此列**：探活走的是 ``conn.adapter.is_connected()``
#: （adapter 槽位），``XTQuantBridge`` 自己并没有这个方法（它只有
#: ``_gateway_connected``）。桥必须有 ``is_connected`` 是因为它兼任 adapter，
#: 由下面的扫描用例覆盖。
_BRIDGE_ASYNC = ("start", "stop", "call", "call_locked")
_BRIDGE_SYNC = ("pump_running", "start_pump_on", "enqueue", "on", "ensure_handler")


def test_bridge_slot_matches_xtquant_bridge():
    """bridge 槽位的每个方法都要有，且**同步/异步形态**必须一致。

    形态不一致比缺失更隐蔽：``await`` 一个同步方法的返回值只会抛
    ``TypeError: object NoneType can't be used in 'await' expression``，
    在启动路径上会被 ``except`` 吞成「连不上」。
    """
    import inspect

    for name in _BRIDGE_ASYNC + _BRIDGE_SYNC:
        assert hasattr(XTQuantBridge, name), f"XTQuantBridge 缺 {name}（测试基线失效）"
        assert hasattr(BigQmtBridge, name), f"BigQmtBridge 缺 bridge 槽位方法 {name}"
        want_async = name in _BRIDGE_ASYNC
        got_async = inspect.iscoroutinefunction(getattr(BigQmtBridge, name))
        assert got_async is want_async, (
            f"{name}: 期望{'async' if want_async else '同步'}，"
            f"实为{'async' if got_async else '同步'}")


def test_pump_running_is_truthful_not_connected():
    """``pump_running()`` 只能回答「泵在不在跑」。

    ★ 它曾经返回 ``self._connected``，于是 ``_pump_guard`` 的
      ``if not b.pump_running(): b.start_pump_on(loop)`` 永远为假 ——
      泵永不启动，大 QMT 事件链路整体静默失效。
    """
    bridge = BigQmtBridge(_FakeConnector(), "c1")
    assert bridge.pump_running() is False          # 未连、未起泵
    bridge._connected = True                        # 只是连上了
    assert bridge.pump_running() is False           # 仍然没泵 —— 判据必须是任务

    async def _run():
        loop = asyncio.get_running_loop()
        bridge.start_pump_on(loop)
        assert bridge.pump_running() is True
        # 幂等：重复调用不得再造一个泵
        first = bridge._pump_task
        bridge.start_pump_on(loop)
        assert bridge._pump_task is first
        bridge.close()
        await asyncio.sleep(0)
        assert bridge.pump_running() is False

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2) 扫描式对齐：产线里用到的 gateway / adapter 方法一个都不能缺
# ---------------------------------------------------------------------------

def test_gateway_covers_every_name_used_in_production():
    """桥上网关必须实现产线代码里出现的**每一个** ``<x>.gateway.<name>``。"""
    used = _attr_names_on("gateway", skip_self_bridge=True)
    assert used, "扫描不到任何 gateway 调用点 —— 扫描逻辑失效（自查）"
    missing = sorted(n for n in used if not hasattr(_BigQmtGateway, n))
    assert not missing, (
        f"产线用到的 gateway 方法在大 QMT 桥上缺失: {missing}；"
        f"（扫描到 {len(used)} 个：{sorted(used)}）")


def test_adapter_covers_every_name_used_in_production():
    """桥作为 ``Connection.adapter`` 时，必须实现产线用到的每个 ``<x>.adapter.<name>``。"""
    used = _attr_names_on("adapter")
    assert used, "扫描不到任何 adapter 调用点 —— 扫描逻辑失效（自查）"
    missing = sorted(n for n in used if not hasattr(BigQmtBridge, n))
    assert not missing, (
        f"产线用到的 adapter 方法在大 QMT 桥上缺失: {missing}；"
        f"（扫描到 {len(used)} 个：{sorted(used)}）")


def test_scan_is_not_silently_empty_and_ignores_string_literals(tmp_path):
    """扫描器自证：真的扫到了产线文件，且**不**把字符串字面量当调用点。

    这是门禁自身的假阴性防线 —— 扫描逻辑一旦写错（路径过滤过狠 / 正则不匹配），
    上面的用例会「因为没有发现缺失而全绿」，比红更危险。
    """
    files = list(_prod_sources())
    assert len(files) > 50, f"只扫到 {len(files)} 个产线文件，路径过滤可能过狠"
    names = _attr_names_on("gateway", skip_self_bridge=True)
    # ``logging.getLogger("qmt_work.gateway.execution")`` 这类字符串不得进集合
    assert "execution" not in names
    # ``XTQuantBridge`` 自身实现里的 ``self.gateway.*`` 不得进集合
    assert "close" not in names, f"自身实现未排除：{sorted(names)}"
    assert "start" not in names, f"自身实现未排除：{sorted(names)}"
    # 反向自证：真的扫到了预期存在的调用点
    assert {"get_orders", "place_order", "get_quote"} <= names


def test_broker_adapter_abstract_surface_is_covered():
    """``BrokerAdapter`` 的抽象方法面（lifecycle + 行情 + 交易）必须齐全。

    刻意从 ABC 的 ``__abstractmethods__`` 取，而不是手抄 —— 抽象基类将来加方法，
    本测试自动跟着变，不需要有人记得来改这里。
    """
    from xtquant_client.base import BrokerAdapter

    missing = sorted(n for n in BrokerAdapter.__abstractmethods__
                     if not hasattr(BigQmtBridge, n))
    assert not missing, f"BigQmtBridge 未实现 BrokerAdapter 的抽象成员: {missing}"


# ---------------------------------------------------------------------------
# 3) call_locked：绝不能在事件循环线程上持 threading.Lock 跨 await（会死锁）
# ---------------------------------------------------------------------------

def test_call_locked_does_not_deadlock_the_event_loop():
    """两个并发写调用必须都能完成。

    ★ 这是死锁回归判据：旧实现用 ``threading.Lock`` + ``await``，第二个写调用
      会把**事件循环线程**阻塞在 ``acquire`` 上，而持锁协程正因为循环被阻塞而
      永远无法恢复 ⇒ 整个后端永久卡死。
    跑在**独立线程 + join 超时**里：真死锁时主测试进程仍然能退出（不会挂住 CI）。
    """
    bridge = BigQmtBridge(_FakeConnector(), "c1")
    done: list[str] = []

    async def _slow(tag):
        await asyncio.sleep(0.15)
        done.append(tag)
        return tag

    async def _main():
        await asyncio.gather(
            bridge.call_locked(_slow, "a"),
            bridge.call_locked(_slow, "b"),
        )

    t = threading.Thread(target=lambda: asyncio.run(_main()), daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "call_locked 在事件循环上死锁（threading.Lock 跨 await）"
    assert sorted(done) == ["a", "b"]


def test_call_locked_serializes_writes():
    """串行化语义必须仍然成立（修完死锁不能把互斥一起修没）。"""
    bridge = BigQmtBridge(_FakeConnector(), "c1")
    order: list[str] = []
    inside = {"n": 0, "max": 0}

    async def _job(tag):
        inside["n"] += 1
        inside["max"] = max(inside["max"], inside["n"])
        await asyncio.sleep(0.05)
        order.append(tag)
        inside["n"] -= 1

    async def _main():
        # ★ 必须**在**协程里构造 gather/wait_for：在 loop 外构造会让它们绑定到
        #   另一个（隐式创建的）事件循环，asyncio.run 时抛
        #   "got Future attached to a different loop"。
        await asyncio.wait_for(asyncio.gather(
            bridge.call_locked(_job, "x"),
            bridge.call_locked(_job, "y"),
            bridge.call_locked(_job, "z"),
        ), timeout=5)

    asyncio.run(_main())
    assert inside["max"] == 1, "写操作未被串行化"
    assert order == ["x", "y", "z"], f"写序被打乱: {order}"


# ---------------------------------------------------------------------------
# 4) 返回容器类型：空 K 线必须是 list 而不是 dict
# ---------------------------------------------------------------------------

class _FakeTransport:
    transport_id = "file"


class _FakeConnector:
    """最小 canonical 连接器：只实现测试用得到的面，返回值全部为空。"""

    transport = _FakeTransport()
    transport_id = "file"

    async def test_connection(self):
        return {"ok": True}

    async def close(self):
        return None

    async def get_kline(self, code, period, count, adjust="", start="", end=""):
        return []

    async def get_quote(self, code):
        return {}

    async def get_positions(self, symbol=None):
        return []

    async def get_orders(self):
        return []

    async def get_deals(self):
        return []

    async def get_account(self):
        return None

    async def get_cash(self):
        return None

    def stream_events(self):
        return []

    def event_semantics(self):
        from connectors.ports import EventSemantics, EventSemanticsSpec

        return EventSemanticsSpec(semantics=EventSemantics.POLL_DIFF,
                                  max_latency_ms=1500)

    def supported_ops(self):
        return ["GET_KLINE"]


@pytest.mark.parametrize("empty", [None, []])
def test_empty_kline_is_list_not_dict(empty):
    """空 K 线必须是 ``[]``。

    ``XTQuantGateway.get_kline`` 契约是 list；曾经这里写 ``or {}``，空结果会
    变成 dict，让 ``kline_io`` 的 ``isinstance(bars, dict) and bars.get("code")``
    分支与 KlineCache 落库拿到错误的容器类型。
    """
    class _C(_FakeConnector):
        async def get_kline(self, code, period, count, adjust="", start="", end=""):
            return empty

    bridge = BigQmtBridge(_C(), "c1")
    bars = asyncio.run(bridge.gateway.get_kline("600000.SH", "1d", 10))
    assert isinstance(bars, list), f"空 K 线返回了 {type(bars).__name__}，应为 list"


def test_bigqmt_only_interfaces_fail_loudly():
    """桥上确实没有的接口（财务 / L2）必须**如实报错**，不得返回空冒充成功。

    返回 ``{}`` / ``[]`` 会被上层解读成「这家公司没有财务数据 / 没有逐笔成交」，
    而真相是「这条通道拿不到」—— 前者会污染基本面结论。
    """
    from xtquant_client.base import BrokerError

    bridge = BigQmtBridge(_FakeConnector(), "c1")
    for coro in (bridge.gateway.get_financial("600000.SH"),
                 bridge.gateway.get_l2_transactions("600000.SH", 10)):
        with pytest.raises(BrokerError):
            asyncio.run(coro)


def test_connector_probe_reports_supported_ops_without_io():
    """诊断面必须零 IO（health 面禁止阻塞）且带上契约对齐所需的清单。"""
    bridge = BigQmtBridge(_FakeConnector(), "c1", connector_key="qmt.big.bridge.file")
    t0 = time.perf_counter()
    probe = bridge.connector_probe()
    assert time.perf_counter() - t0 < 0.2, "connector_probe 不应有耗时 IO"
    assert probe["connector_key"] == "qmt.big.bridge.file"
    assert probe["transport"] == "file"
    assert probe["supported_ops"] == ["GET_KLINE"]
    assert "pump_running" in probe


# ---------------------------------------------------------------------------
# 5) 方言成员对称性（write_ops / supported_ops 三个方言都必须有）
# ---------------------------------------------------------------------------

def test_all_dialects_declare_write_ops_and_supported_ops():
    """三个内置方言必须**都**声明 ``write_ops`` 与 ``supported_ops()``。

    ★ 缺一个，调用方就只能 ``getattr(..., default)`` 兜底，而兜底出来的默认值
      会伪装成正确 —— 「这个方言声明了什么」于是不可观测。
    """
    from connectors.dialects import get_dialect

    for dialect_id in ("xtquant.v1", "bigqmt.v1", "ptrade.v1"):
        d = get_dialect(dialect_id)
        assert isinstance(d.write_ops, frozenset) and d.write_ops, \
            f"{dialect_id} 未声明 write_ops"
        ops = d.supported_ops()
        assert isinstance(ops, tuple) and ops, f"{dialect_id} 未声明 supported_ops"
        # 声明的 op 必须都能被 op_of 翻译（防止两张表漂移）
        for op in ops:
            assert d.op_of(op), f"{dialect_id} 声明了 {op} 但 op_of 翻不出来"


def test_xtquant_dialect_write_ops_reach_inprocess_transport():
    """``XtQuantV1.write_ops`` 必须真的被装配路径用上（不是死配置）。"""
    from connectors.dialects import get_dialect

    d = get_dialect("xtquant.v1")
    assert "place_order" in d.write_ops
    assert "cancel_order" in d.write_ops


def test_generic_connector_exposes_supported_ops():
    """``GenericConnector.supported_ops()`` 由 dialect 派生（诊断面据此对账）。"""
    from connectors.dialects import get_dialect
    from connectors.generic import GenericConnector
    from connectors.transports import InProcessTransport

    class _A:
        def is_connected(self):
            return False

    conn = GenericConnector(
        dialect=get_dialect("bigqmt.v1"),
        transport=InProcessTransport(_A()),
        connector_id="qmt.big.bridge.file")
    ops = conn.supported_ops()
    assert isinstance(ops, list) and "PLACE_ORDER" in ops


# ---------------------------------------------------------------------------
# 参数级对等：方言 prepare 的产出必须能被**每一个**实现签名吃掉
# ---------------------------------------------------------------------------

def _accepted_keywords(fn) -> set:
    """函数能接受的关键字参数名（含 **kwargs 时返回 None 表示「全收」）。"""
    import inspect

    sig = inspect.signature(fn)
    names = set()
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return None      # 全收
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY):
            names.add(p.name)
    return names


def test_place_order_prepare_keys_fit_every_implementation():
    """``XtQuantV1.prepare(PLACE_ORDER)`` 的键必须被所有下单实现接收。

    ★ 这条是「大小 QMT 方法/接口差异」在**参数级**的对等判据。
      生产链路上 ``InProcessTransport`` 是**按关键字**调用 adapter 的
      （``self.bridge.call(fn, **params)``），所以只要任何一个实现的签名漏了
      某个键，就会在运行期抛 ``TypeError``，并被归成 ``SignatureMismatch``
      装配 bug —— 而不是在装配/测试阶段暴露。

      实测踩过：``XTQuantGateway`` 这个 ABC 的 ``place_order`` 声明**比实现窄**，
      漏了 ``strategy_name`` / ``remark``。按 ABC 写的实现会在端口层直接炸。
    """
    from xtquant_client.base import BrokerAdapter
    from xtquant_client.gateway import XTQuantGateway

    from connectors.dialects import get_dialect

    keys = set(get_dialect("xtquant.v1").prepare("PLACE_ORDER", {}).keys())
    assert keys == {"code", "direction", "price_type", "price", "volume",
                    "strategy_name", "remark"}, f"方言 prepare 产出的键变了：{keys}"

    targets = {
        "XTQuantGateway(ABC)": XTQuantGateway.place_order,
        "BrokerAdapter(ABC)": BrokerAdapter.place_order,
        "_BigQmtGateway(大QMT桥 gateway 面)": _BigQmtGateway.place_order,
        "BigQmtBridge(大QMT桥 adapter 面)": BigQmtBridge.place_order,
    }
    for label, fn in targets.items():
        accepted = _accepted_keywords(fn)
        if accepted is None:
            continue
        missing = keys - accepted
        assert not missing, (
            f"{label} 的 place_order 不接受 {sorted(missing)} —— "
            "端口层按关键字调用它时会 TypeError（SignatureMismatch）")


def test_kline_prepare_keys_fit_every_implementation():
    """K 线的关键字映射同样必须两端都能吃下（端口顺序与 adapter 顺序不同）。

    ★ 两个方言的 **wire 参数名刻意不同**，不是漂移：
      * ``xtquant.v1`` 直连 adapter ⇒ 用 adapter 的形参名 ``code``；
      * ``bigqmt.v1``  过 agent 线 ⇒ agent 的 action 参数名是 ``stock_code``。
      所以先各自钉死 wire 名，再验证「进程内直连」那条路的两个实现
      （``XTQuantGateway`` / ``_BigQmtGateway``）都能吃下 ``xtquant.v1`` 的键。
    """
    from xtquant_client.gateway import XTQuantGateway

    from connectors.dialects import get_dialect

    expected = {
        "xtquant.v1": {"code", "period", "count", "start", "end", "adjust"},
        "bigqmt.v1": {"stock_code", "period", "count", "start", "end", "adjust"},
    }
    for dialect_id, want in expected.items():
        keys = set(get_dialect(dialect_id).prepare("GET_KLINE", {}).keys())
        assert keys == want, f"{dialect_id} 的 GET_KLINE 参数键变了：{keys}"

    # 进程内直连路径：这两个实现必须都能按关键字接住 xtquant.v1 的键
    keys = set(get_dialect("xtquant.v1").prepare("GET_KLINE", {}).keys())
    for label, fn in (("XTQuantGateway", XTQuantGateway.get_kline),
                      ("_BigQmtGateway", _BigQmtGateway.get_kline),
                      ("BigQmtBridge", BigQmtBridge.get_kline)):
        accepted = _accepted_keywords(fn)
        assert accepted is None or not (keys - accepted), (
            f"{label}.get_kline 不接受 {sorted(keys - accepted)} —— "
            "InProcessTransport 按关键字调用时会 TypeError（SignatureMismatch）")


def test_bigqmt_gateway_and_bridge_have_identical_public_signatures():
    """``adapter is bridge`` 的对象必须**两套面同名同参**（否则同一段调用代码两条路行为不同）。

    大 QMT 时 ``conn.adapter is conn.bridge``。调用点写
    ``await bridge.call(conn.adapter.get_account)`` 或
    ``await bridge.call(bridge.gateway.get_account)`` 都必须成立。
    """
    import inspect

    gw = _BigQmtGateway
    br = BigQmtBridge
    shared = [n for n in dir(gw) if not n.startswith("_")]
    checked = 0
    for name in shared:
        g = getattr(gw, name, None)
        b = getattr(br, name, None)
        if not callable(g) or not callable(b):
            continue
        try:
            gs = list(inspect.signature(g).parameters)[1:]   # 去掉 self
            bs = list(inspect.signature(b).parameters)[1:]
        except (TypeError, ValueError):
            continue
        assert gs == bs, (
            f"{name}: gateway 参数 {gs} 与 bridge 参数 {bs} 不一致 —— "
            "同一个对象的两套面必须同形，否则调用点无法统一书写")
        checked += 1
    assert checked >= 15, f"只比对了 {checked} 个方法，扫描可能失效"


# ---------------------------------------------------------------------------
# 大小 QMT 的**基类级**接口对等：大 QMT 网关必须能顶替真适配器
# ---------------------------------------------------------------------------

#: ``_BigQmtGateway`` 刻意**不**实现的 ABC 方法：它们由**桥**提供。
#: 大 QMT 时 ``conn.adapter is conn.bridge``，生命周期出入口只有一套
#: （``await bridge.start()`` / ``bridge.close()``）—— 网关再实现一遍就是
#: 「同一个连接有两个 start」，谁先谁后没有定义。
_GATEWAY_LIFECYCLE_ON_BRIDGE = {"start", "close"}


def test_bigqmt_gateway_covers_mini_adapter_abc():
    """大 QMT 网关必须能顶替 mini 的真适配器：ABC 的每个方法都要在（或明确豁免）。

    ★ 这是「大小 QMT 方法/接口差异」在**基类级**的对等判据，与
      ``test_gateway_covers_every_name_used_in_production``（扫产线调用点）互补：
      那条只看「**今天**代码用到的方法」，这条看「契约**声明的**全部方法」——
      功能在 mini 上跑得通、切到大 QMT 却 AttributeError，正是因为声明面比调用面宽。

      判据从 ABC 的 ``__abstractmethods__`` 取，不手抄：抽象基类将来加方法，
      本测试自动跟着变。
    """
    missing = sorted(
        n for n in XTQuantGateway.__abstractmethods__
        if not hasattr(_BigQmtGateway, n) and n not in _GATEWAY_LIFECYCLE_ON_BRIDGE)
    assert not missing, (
        f"大 QMT 网关缺少真适配器契约方法 {missing}：这些功能在 mini 上可用、"
        "切到大 QMT 会直接 AttributeError")
    # 豁免的方法必须真的落在**桥**上，不能两头都没有（否则连接起不来/关不掉）。
    for name in sorted(_GATEWAY_LIFECYCLE_ON_BRIDGE):
        assert hasattr(BigQmtBridge, name), (
            f"{name} 既不在网关上（已豁免）也不在桥上 —— 连接无法启动/关闭")


def test_bigqmt_gateway_accepts_the_same_keywords_as_mini():
    """同名方法的关键字形参名必须与 ABC 一致（关键字调用才不会在一侧 TypeError）。

    生产链路的进程内直连是**按关键字**调用 adapter 的
    （``InProcessTransport`` → ``bridge.call(fn, **params)``），所以形参名漂移
    不会在装配期暴露，只会在运行期炸成 ``SignatureMismatch``。
    """
    import inspect

    diffs = []
    for name in sorted(XTQuantGateway.__abstractmethods__):
        gw = getattr(_BigQmtGateway, name, None)
        abc = getattr(XTQuantGateway, name, None)
        if gw is None or not callable(abc):
            continue
        try:
            pa = [p.name for p in inspect.signature(abc).parameters.values()
                  if p.name != "self" and p.kind != inspect.Parameter.VAR_KEYWORD]
            pb = [p.name for p in inspect.signature(gw).parameters.values()
                  if p.name != "self" and p.kind != inspect.Parameter.VAR_KEYWORD]
        except (TypeError, ValueError):
            continue
        # 大 QMT 网关允许**已登记**的可选加宽（见 _ACCEPTED_GATEWAY_WIDENINGS）：
        # 新增的是带默认值的尾部可选形参，按 ABC 的写法调用不受影响。
        allowed = _ACCEPTED_GATEWAY_WIDENINGS.get(name, set())
        pb_extra = [p for p in pb if p not in pa]
        pb = [p for p in pb if p not in allowed]
        if set(pb_extra) - allowed:
            diffs.append(
                f"{name}: 网关多出未登记的形参 {sorted(set(pb_extra) - allowed)}"
                "（如属有意扩展，请登记进 _ACCEPTED_GATEWAY_WIDENINGS）")
        if pa != pb:
            diffs.append(f"{name}: ABC{pa} vs 大QMT网关{pb}")
    assert not diffs, (
        "形参名/顺序不一致（按关键字调用会在另一侧 TypeError）：\n  "
        + "\n  ".join(diffs))


#: 大 QMT 网关 vs mini **具体适配器** 之间**已声明**的形参加宽。
#: 形式 = {方法名: 只出现在 mini 侧的多余形参集合}。
#: 加宽本身是 Liskov 安全的（mini 收得更多），但**未登记**就会在下次改动后变成
#: 静默 TypeError —— 两侧各跑各的测试都发现不了。
_ACCEPTED_MINI_WIDENINGS = {
    # mini 的 ``subscribe_quote`` 多一个可选 ``period``（周期订阅）。
    # 大 QMT 桥**故意不接**：agent 侧落地形态是轮询 ``get_full_tick``（tick 粒度），
    # 周期传过去只会被无声忽略 —— 「收下却不生效」比「根本没有这个参数」更糟。
    # 与 ABC（真正的契约）签名一致，产线唯一调用点只传 2 个位置参数。
    "subscribe_quote": {"period"},
}

#: 反向加宽登记：**大 QMT 网关**比 mini 具体适配器多出的可选形参。
#: 形式 = {方法名: 多出的形参集合}。Liskov 安全（多的是**带默认值**的可选参数，
#: 按 ABC/mini 的写法调用完全不受影响），但必须显式登记 —— 否则下次改动
#: 会把「有意的能力扩展」和「签名漂移 bug」混在一起，产线 TypeError 无从归因。
_ACCEPTED_GATEWAY_WIDENINGS = {
    # 大 QMT 的 ``passorder`` 支持 ``opAccountType``（股票/ETF/期权/期货/两融），
    # 走扩展 12-arg 签名；mini 底层是 ``trader.order_stock``，**没有**这个形参，
    # 其信用/融资语义由**连接级** account_type 派生。
    # 故本参数只在大 QMT 网关存在，且 `gateway/execution.py::_account_type_arg`
    # 会**按签名探测**后再投递（绝不无条件追加，避免 mini 连接下单 TypeError）。
    "place_order": {"account_type"},
}


def test_bigqmt_gateway_widening_over_the_mini_contract_is_declared():
    """★ 网关相对 mini 侧的形参「加宽」必须**已登记**（否则迟早静默 TypeError）。

    为什么还要单独立一条（另外两条已经保证「方法名都在」「与 ABC 形参名一致」）：
    ABC 与 mini 的**具体适配器**不是同一套词汇 ——
    ``XTPQuantAdapter.__abstractmethods__`` 是**空的**（它实现的是 ``BrokerAdapter``），
    名字也两套（网关侧 ``query_cash``/``query_position``，mini 侧 ``get_cash``/
    ``get_positions``）。于是「实现比 ABC 宽」这条缝**没有任何护栏**：
    谁哪天照着 mini 的签名去调大 QMT 网关，只会在运行期炸 TypeError。

    本用例把可接受的加宽**枚举出来**并与实测对账：新增任何未登记差异即变红。
    """
    import inspect

    from xtquant_client.xtp.adapter import XTPQuantAdapter

    def _params(fn):
        try:
            return [p.name for p in inspect.signature(fn).parameters.values()
                    if p.name != "self" and p.kind != inspect.Parameter.VAR_KEYWORD]
        except (TypeError, ValueError):
            return None

    undeclared = []
    for name in sorted(XTQuantGateway.__abstractmethods__):
        gw, impl = getattr(_BigQmtGateway, name, None), getattr(XTPQuantAdapter, name, None)
        if gw is None or not callable(impl):
            continue                      # 生命周期方法在桥上 / 两套词汇的差异，另有护栏
        pg, pi = _params(gw), _params(impl)
        if pg is None or pi is None:
            continue
        allowed = _ACCEPTED_MINI_WIDENINGS.get(name, set())
        extra = set(p for p in pi if p not in pg)
        if extra != allowed:
            undeclared.append(f"{name}: 大QMT{pg} vs mini{pi}"
                              f"（实得多余 {sorted(extra)}，已登记 {sorted(allowed)}）")
        gw_extra = set(p for p in pg if p not in pi)
        gw_allowed = _ACCEPTED_GATEWAY_WIDENINGS.get(name, set())
        if gw_extra != gw_allowed:
            undeclared.append(
                f"{name}: 大QMT{pg} 相对 mini{pi} 多出 {sorted(gw_extra)}，"
                f"已登记 {sorted(gw_allowed)}")
    assert not undeclared, (
        "网关与 mini 具体适配器的形参加宽未经登记：\n  " + "\n  ".join(undeclared)
        + "\n  若确属有意，请登记进 _ACCEPTED_MINI_WIDENINGS（mini 侧多出）"
          " 或 _ACCEPTED_GATEWAY_WIDENINGS（网关侧多出）并写明理由。")


def test_gateway_never_receives_the_mini_only_period_kwarg():
    """★ 产线里**不得**有人给 ``<x>.gateway.subscribe_quote`` 传 ``period``。

    上一条登记了「mini 侧多一个 ``period``」这条缝；本条是它的落地保证：
    ``period`` 只存在于 mini **具体适配器**，网关上没有 —— 一旦有调用点开始传它，
    大 QMT 会 TypeError，而 mini 侧跑得好好的（**单侧绿**，最难发现的那种）。

    ★ 用 **AST** 而不是正则：正则的 ``[^)]*`` 会停在嵌套调用里第一个 ``)`` 上，
    把 ``f(a, b, c)`` 这种实参误判成「3 个参数」而假报。AST 只看真实调用节点。
    """
    offenders: list[str] = []
    for path in _prod_sources():
        try:
            tree = ast.parse(path.read_text("utf-8", errors="replace"))
        except (OSError, SyntaxError):  # pragma: no cover - IO 竞态 / 非 utf8
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "subscribe_quote"):
                continue
            holder = fn.value
            if not (isinstance(holder, ast.Attribute) and holder.attr == "gateway"):
                continue
            kw = [k.arg for k in node.keywords if k.arg]
            if "period" in kw or len(node.args) > 2:
                offenders.append(
                    f"{path.name}: gateway.subscribe_quote("
                    f"{len(node.args)} 个位置参数, kwargs={kw})")
    assert not offenders, (
        "产线给 gateway.subscribe_quote 传了 mini 专有的 period / 多余位置参数"
        "（大 QMT 会 TypeError，而 mini 侧照常绿）：\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# 6) action 词表五方对账：canonical Ops ↔ 方言 ↔ agent 路由 ↔ agent 自述 ↔ 替身
# ---------------------------------------------------------------------------
#
# 为什么单独立一节（本轮新增）
# ----------------------------
# 走大 QMT 桥时，一次业务的完整链路是：
#
#   BrokerAdapter 方法 → canonical Ops → dialect.op_of() → **wire action**
#     → agent execute() 路由 → 真 QMT 注入函数
#
# 中间那根「wire action」是**纯字符串约定**，两侧各有一张手抄表：
#
#   * 后端侧：``connectors/dialects/bigqmt_v1.py::_OP_TO_ACTION``
#   * agent 侧：``agent_bigqmt/qmt_api.py::_ACTIONS``（自述）+ ``execute()`` 的
#     if 链（真实路由）+ 替身 ``tests/fake_bigqmt_agent.py::_ACTIONS``
#
# 这三处的注释都写着「必须与 X 保持同步 / 逐字对齐」—— 但**没有任何东西在核**。
# 这正是本项目反复踩的「约定写在注释里」的坑：漏一个 action 的后果不是报错，
# 而是运行期回一句 ``未知 action`` / 或 probe 把「已支持」谎报成「不支持」，
# 而两侧的单元测试各自全绿。所以把它做成 AST 对账门禁。
#
# 今天三处恰好一致（14 个），本节的职责是让它**继续**一致。

_AGENT_API = BACKEND / "agent_bigqmt" / "qmt_api.py"
_FAKE_AGENT = BACKEND / "tests" / "fake_bigqmt_agent.py"


def _tuple_literal(path: Path, name: str) -> tuple:
    """取模块级 ``NAME = ("a", "b", ...)`` 的字符串元素（AST，不 import）。"""
    tree = ast.parse(path.read_text("utf-8", errors="replace"))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            continue
        return tuple(e.value for e in node.value.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str))
    raise AssertionError(f"{path.name} 里找不到 {name} 的字符串元组字面量")


def _dispatched_ops(path: Path) -> set:
    """AST 取「真实路由」的 action 集合：比较式左操作数必须是名字 ``op``。

    ★ 必须用 AST：agent 与替身里有若干**非 action** 的字符串比较
      （``envelope.get("signal_id") != name``、``cfg.get("transport")`` 等），
      正则抓 ``== "XXX"`` 会把它们一起收进来，门禁立刻变成噪声。
      限定「左操作数是名为 ``op`` 的变量」既精确又不依赖缩进/顺序。
    """
    tree = ast.parse(path.read_text("utf-8", errors="replace"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "op"):
            continue
        for oper, comp in zip(node.ops, node.comparators):
            if isinstance(oper, ast.Eq) and isinstance(comp, ast.Constant) \
                    and isinstance(comp.value, str):
                out.add(comp.value)
            elif isinstance(oper, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                out.update(e.value for e in comp.elts
                           if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return out


def test_agent_declared_actions_match_its_own_dispatch():
    """agent 自述的 ``_ACTIONS`` 必须等于 ``execute()`` 真正分发的 action 集合。

    两个方向都是真 bug：
    * 自述多一个 → probe 把「不支持」谎报成「支持」，外部端按能力面放行 ⇒ 运行期
      ``未知 action``；
    * 自述少一个 → 真能跑的能力被标 UNKNOWN，能力面反向假阴性（同样掩盖真相）。
    """
    declared = set(_tuple_literal(_AGENT_API, "_ACTIONS"))
    dispatched = _dispatched_ops(_AGENT_API)
    assert dispatched, "AST 没扫到任何 action 路由 —— 扫描逻辑失效（自查）"
    assert declared == dispatched, (
        f"agent 自述 _ACTIONS 与 execute() 路由漂移：\n"
        f"  只在自述里: {sorted(declared - dispatched)}\n"
        f"  只在路由里: {sorted(dispatched - declared)}")


def test_bigqmt_dialect_actions_match_the_agent_surface():
    """后端方言会同意的 action 集合，必须与 agent 真正认的**完全一致**。

    * 方言会发、agent 不认 ⇒ 该功能在桥接形态下**必挂**，且只在真机上暴露；
    * agent 认得、方言永不发 ⇒ 死代码（能力面却可能按 agent 自述把它标成 SUPPORTED）。
    """
    from connectors.dialects.bigqmt_v1 import _OP_TO_ACTION

    dial = set(_OP_TO_ACTION.values())
    agent = _dispatched_ops(_AGENT_API)
    assert dial == agent, (
        f"bigqmt.v1 方言与 agent 的 action 词表漂移：\n"
        f"  方言会发但 agent 不认: {sorted(dial - agent)}\n"
        f"  agent 认但方言永不发: {sorted(agent - dial)}")


def test_fake_agent_action_surface_matches_the_real_one():
    """契约替身不得比真身宽或窄 —— 否则测试证明的是替身的行为，不是 agent 的。

    * 替身比真身**窄**：真 agent 支持的 action 无人在测（假绿）；
    * 替身比真身**宽**：测试在一个真机上不存在的 action 上「全绿」（更假）。
    """
    real = set(_tuple_literal(_AGENT_API, "_ACTIONS"))
    fake_decl = set(_tuple_literal(_FAKE_AGENT, "_ACTIONS"))
    fake_disp = _dispatched_ops(_FAKE_AGENT)
    assert fake_decl == real, (
        f"替身自述 _ACTIONS 与真 agent 不一致：\n"
        f"  替身多: {sorted(fake_decl - real)}\n  替身少: {sorted(real - fake_decl)}")
    assert fake_disp == real, (
        f"替身**路由**与真 agent 不一致（路由才是真实行为）：\n"
        f"  替身多: {sorted(fake_disp - real)}\n  替身少: {sorted(real - fake_disp)}")


def test_action_alignment_scan_is_not_silently_empty():
    """门禁自身防线：扫描器必须真的读到内容，且不能把无关比较算成 action。"""
    dispatched = _dispatched_ops(_AGENT_API)
    assert len(dispatched) >= 14, f"只扫到 {len(dispatched)} 个 action，扫描可能失效"
    # 这些字符串在 qmt_api.py 里以**非 action** 身份出现，绝不能被收进来
    assert "Rejected" not in dispatched, f"扫描把 error_type 当成了 action：{sorted(dispatched)}"
    assert "subscribe_quote" not in dispatched


#: 已登记的「模板方言」缺口。``ptrade.v1`` 是 Phase 4-a 的**接入模板**（docstring 明说
#: 未经实机验证），缺的 op 一律走 ``UnsupportedOp``（能力面诚实标 UNKNOWN）。
#: 登记而非豁免：谁哪天补齐了其中一个，本用例立刻红，逼着更新登记 —— 反过来，
#: 谁想「顺手删一个」也会红。
_TEMPLATE_DIALECT_GAPS = {
    "ptrade.v1": {"GET_FULL_TICK", "GET_INSTRUMENT_DETAIL",
                  "GET_SECTOR_LIST", "SUBSCRIBE_QUOTE"},
}


def test_production_dialects_translate_every_canonical_op():
    """两个**产线**方言必须覆盖全部 canonical ops；模板方言的缺口必须已登记。

    新增一个 ``Ops.*`` 常量却忘了在方言里翻译时，这条会红 ——
    否则症状是运行期 ``UnsupportedOp``（能力缺口伪装成「券商不支持」）。
    """
    from connectors.dialects import get_dialect
    from connectors.dialects.base import Ops

    canonical = {v for k, v in vars(Ops).items()
                 if not k.startswith("_") and isinstance(v, str)}
    assert len(canonical) >= 15, f"canonical Ops 只解析到 {len(canonical)} 个（自查失效）"

    for dialect_id in ("xtquant.v1", "bigqmt.v1"):
        missing = canonical - set(get_dialect(dialect_id).supported_ops())
        assert not missing, f"产线方言 {dialect_id} 缺少 canonical op: {sorted(missing)}"

    for dialect_id, allowed in _TEMPLATE_DIALECT_GAPS.items():
        actual = canonical - set(get_dialect(dialect_id).supported_ops())
        assert actual == allowed, (
            f"{dialect_id} 的未覆盖 op 与登记不一致：\n"
            f"  实测: {sorted(actual)}\n  登记: {sorted(allowed)}\n"
            f"  （补齐了就更新 _TEMPLATE_DIALECT_GAPS，别让它变成过期说明）")


# ---------------------------------------------------------------------------
# 7) 存活判据：agent 死掉后 is_connected() 必须如实转 False
# ---------------------------------------------------------------------------
#
# 旧实现 ``is_connected() -> self._connected``，而 ``_connected`` 只在 start()
# 握手成功时置真、只在显式 stop()/close() 置假 ⇒ **agent 进程死掉后它永远是真**：
# 前端显示「已连接」、``gateway.health`` 永不判掉线、``_reconnect`` 永不被调度。
# 修复后：泵在有界退避下主动探活，连续两次无应答即置 ``_agent_unresponsive``。
#
# 本节的用例刻意**只走公开行为**（``is_connected`` / ``connector_probe`` /
# 泵的接线），只在「模拟 30s 退避到期」这一处动内部字段，并把理由写在注释里。

class _FlakyConnector(_FakeConnector):
    """可切换「agent 是否应答」的连接器替身。

    ``test_connection`` 接受 ``timeout``：真实 ``GenericConnector`` 支持这个短预算
    （``_call_with_optional_timeout`` 会优先用它），替身跟着支持才能验证超时确实
    被传下去。
    """

    def __init__(self) -> None:
        self.answer = True
        self.calls = 0
        self.timeouts: list = []

    async def test_connection(self, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        if not self.answer:
            from connectors.transport import TransportError

            raise TransportError("[file] PROBE 超时 5.0s 未收到响应")
        return {"ok": True}


def _idle_and_expire(bridge) -> None:
    """模拟「90 秒无任何成功往返 + 30s 退避已到期」。

    ★ 这是本节唯一触碰内部状态的地方（其余用例只走公开行为）。必须**同时**清两个
      字段，因为它们守的是两件事：
        * ``_last_agent_ok``    —— 空闲闸门（有近期往返就根本不探活）；
        * ``_liveness_next_at`` —— 失败退避闸门。
      只清后者时，前一次成功后留下的新鲜存活戳仍会让探活**故意**不发请求
      （那正是 ``test_liveness_probe_stays_silent_when_business_traffic_is_recent``
      要保的语义），用例就会误判成「没有探活」。
    """
    bridge._last_agent_ok = 0.0
    bridge._liveness_next_at = 0.0


def test_is_connected_goes_false_when_agent_stops_answering():
    """agent 连续无应答 ⇒ ``is_connected()`` 如实返回 False（假绿灯修复）。"""
    conn = _FlakyConnector()
    bridge = BigQmtBridge(conn, "c1")
    bridge._connected = True                     # 模拟「曾经握手成功」
    assert bridge.is_connected() is True

    conn.answer = False
    asyncio.run(bridge._liveness_probe())
    assert bridge.is_connected() is True, "单次无应答不应立刻判死（瞬时抖动）"
    assert conn.calls == 1

    _idle_and_expire(bridge)
    asyncio.run(bridge._liveness_probe())
    assert conn.calls == 2, "退避到期后必须重试，否则永远等不到自愈"
    assert bridge.is_connected() is False, "连续两次无应答后仍报「已连接」= 假绿灯"

    # 诊断面必须能看出「不是没连过，而是 agent 不回话了」。
    probe = bridge.connector_probe()
    assert probe["agent_unresponsive"] is True
    assert probe["available"] is False
    assert probe["liveness_failures"] == 2


def test_any_successful_roundtrip_clears_the_unresponsive_flag():
    """一次成功的真实往返必须立刻恢复「可用」，无需重启连接。"""
    conn = _FlakyConnector()
    bridge = BigQmtBridge(conn, "c1")
    bridge._connected = True
    conn.answer = False
    asyncio.run(bridge._liveness_probe())
    _idle_and_expire(bridge)
    asyncio.run(bridge._liveness_probe())
    assert bridge.is_connected() is False

    conn.answer = True
    _idle_and_expire(bridge)
    asyncio.run(bridge._liveness_probe())
    assert bridge.is_connected() is True
    assert bridge.connector_probe()["agent_unresponsive"] is False

    # 业务往返（``call``）同样算证据 —— 否则恢复只能等下一个探活周期，白等 30s。
    conn.answer = False
    _idle_and_expire(bridge)
    asyncio.run(bridge._liveness_probe())
    _idle_and_expire(bridge)
    asyncio.run(bridge._liveness_probe())
    assert bridge.is_connected() is False, "连续两次无应答后应判不可用"
    asyncio.run(bridge.call(lambda: "ok"))       # 一次业务往返
    assert bridge.is_connected() is True, "成功往返未能解除不可用标记"


def test_liveness_probe_stays_silent_when_business_traffic_is_recent():
    """近期有过成功往返时**不得**多发探活请求（正常路径零额外开销）。"""
    conn = _FlakyConnector()
    bridge = BigQmtBridge(conn, "c1")
    bridge._connected = True
    asyncio.run(bridge.call(lambda: "ok"))        # 一次业务往返
    conn.calls = 0
    for _ in range(3):
        asyncio.run(bridge._liveness_probe())
    assert conn.calls == 0, "有空闲判据却不生效：每分钟都在白跑 PROBE"


def test_liveness_probe_passes_the_short_timeout_budget():
    """探活必须用**短**超时：否则 agent 死掉时每轮都要等满 30s，把事件派发拖住。"""
    from connectors.bigqmt_bridge import _LIVENESS_PROBE_TIMEOUT
    from connectors.transport import MAX_TIMEOUT

    assert 0 < _LIVENESS_PROBE_TIMEOUT < MAX_TIMEOUT
    conn = _FlakyConnector()
    bridge = BigQmtBridge(conn, "c1")
    bridge._connected = True
    asyncio.run(bridge._liveness_probe())
    assert conn.timeouts == [_LIVENESS_PROBE_TIMEOUT]


def test_liveness_backoff_is_not_a_permanent_lock():
    """退避只推迟下一次探活，不能变成永久锁（否则 agent 恢复后永不判活）。"""
    from connectors.bigqmt_bridge import _LIVENESS_RETRY_SECONDS

    conn = _FlakyConnector()
    bridge = BigQmtBridge(conn, "c1")
    bridge._connected = True
    asyncio.run(bridge._liveness_probe())
    assert conn.calls == 1
    asyncio.run(bridge._liveness_probe())         # 退避未到期
    assert conn.calls == 1, "退避闸门未生效"
    assert bridge._liveness_next_at > 0, "退避到期的时刻必须被记下来（不是布尔锁）"
    assert 0 < _LIVENESS_RETRY_SECONDS <= 60.0


def test_pump_actually_runs_the_liveness_probe(monkeypatch):
    """★ 接线判据：泵循环里必须真的调用探活。

    上面几条验的是「探活对不对」，这条验的是「探活有没有被调」——
    本项目的教训是**逻辑写好了但没人调**（A9 的 ``SUB_QUOTE`` 从未发出、
    Gate 1 无人读），所以「有没有接线」必须单独断言。
    """
    import connectors.bigqmt_bridge as mod

    monkeypatch.setattr(mod, "_LIVENESS_FAILURES_TO_DIE", 1)
    conn = _FlakyConnector()
    conn.answer = False
    bridge = BigQmtBridge(conn, "c1")
    bridge._connected = True

    async def _run():
        bridge.start_pump_on(asyncio.get_running_loop())
        for _ in range(40):                       # 泵的心跳是 1s，最多等 ~2s
            if not bridge.is_connected():
                break
            await asyncio.sleep(0.05)
        running_before_close = bridge.pump_running()
        bridge.close()
        await asyncio.sleep(0)
        return running_before_close

    was_running = asyncio.run(_run())
    assert was_running is True
    assert conn.calls >= 1, "泵没有跑探活 ⇒ is_connected 会永远停在 True（假绿灯回归）"
    assert bridge.is_connected() is False


# =====================================================================
# 2026-10-03：ensure_handler 幂等性（行情帧放大 / handler 无界累积）
# =====================================================================
# 背景：``BigQmtBridge.ensure_handler`` 原先直接 ``self.on()``（无条件 append），
# 而唯一调用方 ``app.bootstrap.phase_watchdogs._pump_guard`` 每 **2 秒**无条件调用一次。
# 后果（不是理论风险，是运行期确定性退化）：
#   1. 大 QMT 连接保持活跃期间 ``_handlers["quote"]`` 每天追加 ≈43,200 个重复引用，
#      进程不重启就持续膨胀（内存泄漏）；
#   2. ``enqueue`` 遍历全表 ⇒ 一条行情帧被派发 N 次 ⇒ ``SyncEngine.on_event`` 执行 N 次
#      ⇒ K 线重复落库、内存队列重复入队、下游指标重复计算。
# 同一仓库 ``xtquant_client.gateway.XTQuantBridge.ensure_handler`` 已经是幂等实现
# （同一调用方、同一语义），此前两个实现行为相反。
def test_bigqmt_ensure_handler_is_idempotent():
    b = BigQmtBridge.__new__(BigQmtBridge)
    b._handlers = {}
    delivered: list[dict] = []
    handler = lambda evt: delivered.append(evt)

    # 模拟 _pump_guard 的 2 秒周期重复调用
    for _ in range(5000):
        b.ensure_handler("quote", handler)

    assert b._handlers["quote"] == [handler], (
        f"ensure_handler 未去重：同一 handler 被追加 {len(b._handlers['quote'])} 次"
        f"（_pump_guard 每 2s 一次，长期运行将无界膨胀）")

    # 一帧只派发一次
    b.enqueue({"type": "quote", "data": {"code": "600000.SH"}})
    assert len(delivered) == 1, (
        f"单帧被派发 {len(delivered)} 次（修复前为追加次数，行情帧放大 ⇒ 重复落库）")


def test_two_ensure_handler_implementations_agree_on_idempotency():
    """两个桥的 ensure_handler 语义必须一致——同调用方、同语义，不允许各写一套。

    回归保护：本次缺陷的根因正是两份实现语义相反（一个幂等、一个 append）。
    如果将来再有人「对齐」到 append 语义，本测试会立刻红。

    用**未绑定函数**直接调用而非实例化：这里只测 handler 注册行为，
    给它一个只提供 ``_handlers`` 的合成 self 即可（两个桥的 ``__init__``
    都拉起线程池/事件循环，实例化成本与副作用都远大于被测逻辑本身）。
    """
    class _Self:
        def __init__(self):
            self._handlers = {}

    cases = [
        ("bigqmt", BigQmtBridge.ensure_handler),
        # ★ 是 XTQuantBridge，不是 XTQuantGateway —— 后者是带 10 个抽象方法的 ABC，
        #   既没有 ensure_handler，也无法实例化。xtquant 侧的幂等实现住在桥里。
        ("xtquant", XTQuantBridge.ensure_handler),
    ]
    for label, method in cases:
        obj = _Self()
        h = lambda evt: None
        for _ in range(200):
            method(obj, "quote", h)
        assert len(obj._handlers["quote"]) == 1, (
            f"{label}.ensure_handler 未幂等：{len(obj._handlers['quote'])} 个引用")


def test_ensure_handler_does_dedup_across_distinct_instances():
    """两个**不同** handler（如两条连接各自的回调）都必须保留，不被误删。"""
    b = BigQmtBridge.__new__(BigQmtBridge)
    b._handlers = {}
    got: list = []
    h1 = lambda evt: got.append("h1")
    h2 = lambda evt: got.append("h2")
    for _ in range(50):
        b.ensure_handler("quote", h1)
        b.ensure_handler("quote", h2)
    assert sorted(b._handlers["quote"], key=id) and len(b._handlers["quote"]) == 2
    b.enqueue({"type": "quote"})
    assert got == ["h1", "h2"]


def test_bigqmt_emits_disconnected_once_when_agent_becomes_unresponsive(monkeypatch):
    """大 QMT 侧的掉线必须经同一 ``broker.disconnected`` 链路送到前端。

    回归保护：2026-10-03。此前 xtquant 适配器有 SDK 的 ``on_disconnected`` 回调，
    而大 QMT 桥只有「可用性布尔量」——没有「可用性由可用变不可用」这个**边沿事件**，
    前端只能靠下一次健康轮询才发现（延迟 = 探活间隔 × 失败阈值）。

    断言四件事：
      1. 连败**到阈值的那一次**才发事件（未达阈值只记账，不骚扰前端）；
      2. 只发**一次**（边沿去重：失败持续期间不刷屏）；
      3. ``is_connected()`` 如实反映状态，与事件一致；
      4. 恢复后重新计数，**再次**连败仍能触发新边沿（不是一次性全局开关）。
    """
    class _Flaky:
        def __init__(self):
            self.calls = 0
            self.ok = True

        async def test_connection(self):
            self.calls += 1
            if not self.ok:
                raise OSError("agent 已死")

    # 三个探活节奏常量必须压平，否则第 2 次探活会被
    # ``now < _liveness_next_at`` 短路，失败永远累计不到阈值。
    monkeypatch.setattr("connectors.bigqmt_bridge._LIVENESS_IDLE_SECONDS", 0)
    monkeypatch.setattr("connectors.bigqmt_bridge._LIVENESS_RETRY_SECONDS", 0)
    monkeypatch.setattr("connectors.bigqmt_bridge._LIVENESS_FAILURES_TO_DIE", 2)

    connector = _Flaky()
    bridge = BigQmtBridge.__new__(BigQmtBridge)
    bridge.connector = connector
    bridge.conn_id = "c1"
    bridge._connected = True
    bridge._handlers = {}
    bridge._last_agent_ok = 0.0        # 0.0 为假值 ⇒ 跳过 idle 短路
    bridge._liveness_next_at = 0.0
    bridge._liveness_failures = 0
    bridge._agent_unresponsive = False

    delivered: list[dict] = []
    # ★ handler 必须在**探活之前**注册：边沿事件是在第 2 次探活时同步派发的，
    #   事后再注册永远收不到（这就是旧版本测试「断言不了任何东西」的原因）。
    bridge.ensure_handler("disconnected", lambda e: delivered.append(e))

    async def probe(n):
        for _ in range(n):
            await bridge._liveness_probe()

    connector.ok = False
    asyncio.run(probe(1))               # 第 1 次失败：只记账
    assert bridge._liveness_failures == 1
    assert bridge._agent_unresponsive is False
    assert delivered == [], "阈值未到就发事件会骚扰前端"
    assert bridge.is_connected() is True

    asyncio.run(probe(1))               # 第 2 次失败 ⇒ 翻真 ⇒ 边沿
    assert bridge._agent_unresponsive is True
    assert len(delivered) == 1, "翻真的那一刻必须发出 disconnected"
    assert delivered[0]["type"] == "disconnected"
    assert "agent 已死" in delivered[0]["detail"]
    assert bridge.is_connected() is False

    asyncio.run(probe(5))               # 失败持续 ⇒ 只发一次
    assert len(delivered) == 1, (
        f"边沿被重复触发 {len(delivered)} 次（刷屏/重复落库）")

    # 恢复：一次成功即清零，且**能再次**触发新边沿
    connector.ok = True
    asyncio.run(probe(1))
    assert bridge._agent_unresponsive is False
    assert bridge._liveness_failures == 0
    assert bridge.is_connected() is True
    connector.ok = False
    asyncio.run(probe(2))
    assert len(delivered) == 2, "恢复后再次连败必须能再次发出边沿"
    assert delivered[1]["type"] == "disconnected"


def test_bigqmt_disconnected_event_registered_via_adapter_slot():
    """``on_disconnect`` 必须是大 QMT 桥的公开方法（接口门禁 + 行为双保险）。"""
    b = BigQmtBridge.__new__(BigQmtBridge)
    b._disconnect_cbs = []
    b.on_disconnect(lambda: None)
    assert len(b._disconnect_cbs) == 1
    # None 不得污染注册表
    b.on_disconnect(None)
    assert len(b._disconnect_cbs) == 1
