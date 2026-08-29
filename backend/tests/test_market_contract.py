"""J3 契约测试：新增市场端点（E3/F4/G2/G3）字段结构与降级路径。

设计原则（与项目铁律一致）：
- 真实数据、零 mock：所有断言基于真实返回的 JSON 形状，不构造任何假数据。
- 字段契约优先：新端点必须返回约定字段；缺失字段即契约破坏。
- 降级路径：未知/越界参数不得 500，须优雅返回（code=0 + 空/回落）。

运行方式（任选）：
    # 作为 pytest 运行（需要后端存活）
    python -m pytest tests/test_market_contract.py -v
    # 指定后端地址
    QMT_BASE=http://127.0.0.1:21120/api/v1 python -m pytest tests/test_market_contract.py -v
    # 作为普通脚本运行（无需 pytest）
    python tests/test_market_contract.py

后端不可达时：pytest 模式整模块 skip；standalone 模式打印 SKIP 并退出码 0。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (os.environ.get("QMT_BASE") or "http://127.0.0.1:21118/api/v1").rstrip("/")

_NUM = (int, float)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    with _opener().open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with _opener().open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _reachable():
    try:
        req = urllib.request.Request(BASE + "/market/overview")
        req.add_header("Accept", "application/json")
        with _opener().open(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def _env_ok(resp):
    return isinstance(resp, dict) and resp.get("code") == 0 and "data" in resp


# ============================ E3 市场概览 ============================
def test_e3_envelope_and_keys():
    d = get("/market/overview")
    assert _env_ok(d), f"E3 包异常: {d}"
    data = d["data"]
    for k in ("breadth", "indices", "breadth_trend", "two_city_turnover", "source"):
        assert k in data, f"E3 缺字段 {k}"


def test_e3_breadth_real_counts():
    data = get("/market/overview")["data"]
    assert isinstance(data["breadth"], list) and len(data["breadth"]) >= 1
    for b in data["breadth"]:
        assert b.get("code") and b.get("name")
        # 统计类板块真实口径：count 为家数，unit 为「家」
        assert b.get("metric") == "count"
        assert b.get("unit") == "家"
        assert isinstance(b.get("count"), _NUM)


def test_e3_indices_snapshot():
    data = get("/market/overview")["data"]
    assert isinstance(data["indices"], list) and len(data["indices"]) >= 1
    for it in data["indices"][:8]:
        assert it.get("code")
        assert isinstance(it.get("last"), _NUM)
        assert isinstance(it.get("change_pct"), _NUM)


def test_e3_two_city_turnover_contract():
    # C4：两市成交额 = 上证(000001.SH)+深证(399001.SZ) 指数快照 amount 求和（真实口径）。
    # 任一市场缺 amount 时整体为 None（显「—」，不造假）；有值则非负数字。
    data = get("/market/overview")["data"]
    t = data.get("two_city_turnover")
    assert t is None or (isinstance(t, _NUM) and t >= 0), f"two_city_turnover 类型异常: {t!r}"
    assert "two_city_note" in data, "overview 缺 two_city_note 说明"


def test_e3_breadth_trend_optional_shape():
    data = get("/market/overview")["data"]
    bt = data.get("breadth_trend")
    # 允许 None（源间歇）；有值则为 {code,name,series:[{date,value}]}
    if bt is not None:
        assert isinstance(bt, dict)
        series = bt.get("series") or []
        assert isinstance(series, list)
        for p in series:
            assert "date" in p and "value" in p


# ============================ G2 板块资金流 ============================
def test_g2_envelope_and_aggregation():
    d = get("/market/board/moneyflow", code="881319.SH", top_n=5)
    assert _env_ok(d), f"G2 包异常: {d}"
    data = d["data"]
    for k in ("code", "count", "total_net", "total_inside", "total_outside",
              "contributors", "granularity", "source"):
        assert k in data, f"G2 缺字段 {k}"
    assert data["granularity"] == "day"  # eltdx 仅日累计口径
    assert isinstance(data["count"], int) and data["count"] > 0
    assert isinstance(data["total_net"], _NUM)


def test_g2_contributors_shape():
    data = get("/market/board/moneyflow", code="881319.SH", top_n=5)["data"]
    cons = data["contributors"]
    assert isinstance(cons, list) and len(cons) > 0
    for c in cons:
        assert c.get("code") and c.get("name")
        assert isinstance(c.get("net"), _NUM)
        assert isinstance(c.get("pct"), _NUM)  # 贡献度 = net/|total_net|*100
    assert sum(abs(c["pct"]) for c in cons) > 0


def test_g2_invalid_code_no_500():
    # 未知/非板块码不得 500 也不得长时间挂起：非 881/880 段直接 400 快失败
    # （源层会对未知码做全市场回落聚合 >90s，属缺陷，端点已前置域校验拦截）。
    d = get("/market/board/moneyflow", code="999999.SH", top_n=3)
    assert d.get("code") == 400, f"G2 非板块码应 400 快失败，实际: {d}"
    assert "881" in (d.get("message") or "") or "板块" in (d.get("message") or "")


# ============================ F4 板块轮动 ============================
def test_f4_envelope_and_matrix():
    d = get("/market/rotation", days=5, top_n=10, kind="industry")
    assert _env_ok(d), f"F4 包异常: {d}"
    data = d["data"]
    assert data["days"] == 5
    assert data["kind"] == "industry"
    boards = data["boards"]
    assert isinstance(boards, list) and len(boards) > 0
    for b in boards:
        assert b.get("code") and b.get("name")
        assert isinstance(b.get("total_pct"), _NUM)
        assert isinstance(b.get("cum_pct"), _NUM)
        daily = b.get("daily")
        assert isinstance(daily, list) and len(daily) == 5
        for day in daily:
            assert "date" in day and "pct" in day


def test_f4_param_bounds():
    # days 越界应被钳制或拒绝（不崩溃）
    d = get("/market/rotation", days=999, top_n=10, kind="industry")
    assert d.get("code") == 0, f"F4 越界参数应优雅处理: {d}"
    # top_n 上限保护
    d2 = get("/market/rotation", days=3, top_n=500, kind="concept")
    assert d2.get("code") == 0, f"F4 top_n 越界应优雅处理: {d2}"


# ============================ G3 资金流落库回放 ============================
def test_g3_snapshot_insert_and_replay():
    p = post("/market/moneyflow/snapshot", {"codes": ["600519.SH", "000001.SZ"]})
    assert _env_ok(p), f"G3 快照失败: {p}"
    assert p["data"]["inserted"] == 2
    r = get("/market/moneyflow/replay", code="600519.SH", limit=5)
    assert _env_ok(r), f"G3 回放失败: {r}"
    rows = r["data"]["rows"]
    assert isinstance(rows, list) and len(rows) >= 1
    row = rows[0]
    for k in ("code", "ts", "inside", "outside", "net"):
        assert k in row, f"G3 回放缺字段 {k}"
    assert isinstance(row["net"], _NUM)


def test_g3_replay_unknown_code_no_error():
    r = get("/market/moneyflow/replay", code="999999.XX", limit=5)
    assert r.get("code") == 0, f"G3 未知码回放应优雅: {r}"
    assert isinstance(r["data"]["rows"], list)


# ============================ standalone runner ============================
def _standalone():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    print(f"契约测试：BASE={BASE}")
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f"  [OK  ] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__} — {type(e).__name__}: {e}")
    print(f"\n结果: {passed} pass / {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    if not _reachable():
        print(f"SKIP: 后端不可达（{BASE}），跳过契约测试。")
        sys.exit(0)
    sys.exit(_standalone())
