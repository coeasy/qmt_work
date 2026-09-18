"""成交去重指纹回归（阶段 3 P1-10）。

★ 为什么单独锁：成交回报有**两条路径** —— 轮询补齐 与 实时回报推送。
原先两处各自拼键（`seq or deal_id` + `time` vs `seq or trade_id` + `trade_time`），
字段命名习惯不同 ⇒ **同一笔真实成交被算成两笔**，界面上表现为成交明细重复、
成交笔数与成交量虚高。现在统一走 `sync.deal_fingerprint` 唯一入口。

锁定的是「两种字段写法必须算出同一个键」，这正是原 bug 的失效点。
"""
from sync import deal_fingerprint


def test_two_legacy_shapes_produce_same_key():
    """旧的两套字段写法（deal_id+time / trade_id+trade_time）必须同键。"""
    a = {"order_id": "1001", "seq": 2, "price": 10.5, "volume": 100, "time": "09:31:05"}
    b = {
        "order_id": "1001",
        "trade_id": 2,
        "price": 10.5,
        "volume": 100,
        "trade_time": "09:31:05",
    }
    assert deal_fingerprint(a) == deal_fingerprint(b)


def test_different_deals_produce_different_keys():
    """反例护栏：去重不能过猛 —— 不同成交必须区分开。"""
    base = {"order_id": "1001", "seq": 2, "price": 10.5, "volume": 100, "time": "09:31:05"}
    assert deal_fingerprint(base) != deal_fingerprint({**base, "seq": 3})
    assert deal_fingerprint(base) != deal_fingerprint({**base, "volume": 200})
    assert deal_fingerprint(base) != deal_fingerprint({**base, "price": 10.6})
    assert deal_fingerprint(base) != deal_fingerprint({**base, "order_id": "1002"})


def test_missing_fields_degrade_but_stay_stable():
    """缺字段时不能崩，且同一份残缺数据多次调用结果稳定（否则去重失效）。"""
    sparse = {"order_id": "1001", "price": 10.5}
    k1 = deal_fingerprint(sparse)
    k2 = deal_fingerprint(dict(sparse))
    assert k1 == k2
    assert isinstance(k1, tuple)
    # 空字典也不应抛异常（防御：脏数据不能打挂成交推送链路）
    assert deal_fingerprint({}) == ("", "", "None", "None", "")


def test_numeric_and_string_forms_agree():
    """价格/数量有时是 float 有时是 str（不同数据源口径），去重不应被类型差异骗过。"""
    a = {"order_id": "1", "seq": 1, "price": 10.5, "volume": 100, "time": "t"}
    b = {"order_id": "1", "seq": 1, "price": "10.5", "volume": "100", "time": "t"}
    assert deal_fingerprint(a) == deal_fingerprint(b)
