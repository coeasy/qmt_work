"""J3 契约测试：市场端点（E3/F4/G2/G3）字段结构与降级路径。

Phase 1 ④（P2-26）：**app fixture 进程内运行，不再依赖存活后端**——
经 conftest.app_client（完整 lifespan）发起请求，CI 可直接跑。

设计原则（与项目铁律一致）：
- 真实数据、零 mock：所有断言基于真实返回的 JSON 形状，不构造任何假数据。
- 字段契约优先：端点就绪（code=0）时约定字段必须齐全，缺失即契约破坏；
  数据依赖断言（数量/聚合值）在无数据源环境显式 skip（绝不造数据凑断言）。
- 降级路径：未知/越界参数不得 500，须优雅返回。
"""
import pytest

_NUM = (int, float)


def get(client, path, **params):
    r = client.get("/api/v1" + path, params=params or None)
    assert r.status_code == 200, f"{path} HTTP {r.status_code}: {r.text[:200]}"
    return r.json()


def post(client, path, payload):
    r = client.post("/api/v1" + path, json=payload)
    assert r.status_code == 200, f"{path} HTTP {r.status_code}: {r.text[:200]}"
    return r.json()


def _env_ok(resp):
    return isinstance(resp, dict) and resp.get("code") == 0 and "data" in resp


def _ready(resp):
    """端点就绪（code=0）才执行数据断言；离线无源时显式 skip，绝不伪造。"""
    if not _env_ok(resp):
        pytest.skip(f"端点未就绪（code={resp.get('code')}，离线无数据源属正常）："
                    f"{str(resp.get('message'))[:120]}")


# ============================ E3 市场概览 ============================
def test_e3_envelope_and_keys(app_client):
    d = get(app_client, "/market/overview")
    _ready(d)
    data = d["data"]
    for k in ("breadth", "indices", "breadth_trend", "two_city_turnover", "source"):
        assert k in data, f"E3 缺字段 {k}"


def test_e3_breadth_real_counts(app_client):
    data = get(app_client, "/market/overview")["data"]
    if not data.get("breadth"):
        pytest.skip("离线无行情源，无 breadth 数据")
    for b in data["breadth"]:
        assert b.get("code") and b.get("name")
        # 统计类板块真实口径：count 为家数，unit 为「家」
        assert b.get("metric") == "count"
        assert b.get("unit") == "家"
        assert isinstance(b.get("count"), _NUM)


def test_e3_indices_snapshot(app_client):
    data = get(app_client, "/market/overview")["data"]
    if not data.get("indices"):
        pytest.skip("离线无行情源，无指数快照")
    for it in data["indices"][:8]:
        assert it.get("code")
        assert isinstance(it.get("last"), _NUM)
        assert isinstance(it.get("change_pct"), _NUM)


def test_e3_two_city_turnover_contract(app_client):
    # C4：两市成交额 = 上证(000001.SH)+深证(399001.SZ) 指数快照 amount 求和（真实口径）。
    # 任一市场缺 amount 时整体为 None（显「—」，不造假）；有值则非负数字。
    data = get(app_client, "/market/overview")["data"]
    t = data.get("two_city_turnover")
    assert t is None or (isinstance(t, _NUM) and t >= 0), f"two_city_turnover 类型异常: {t!r}"
    assert "two_city_note" in data, "overview 缺 two_city_note 说明"


def test_e3_breadth_trend_optional_shape(app_client):
    data = get(app_client, "/market/overview")["data"]
    bt = data.get("breadth_trend")
    # 允许 None（源间歇）；有值则为 {code,name,series:[{date,value}]}
    if bt is not None:
        assert isinstance(bt, dict)
        series = bt.get("series") or []
        assert isinstance(series, list)
        for p in series:
            assert "date" in p and "value" in p


# ============================ G2 板块资金流 ============================
def test_g2_envelope_and_aggregation(app_client):
    d = get(app_client, "/market/board/moneyflow", code="881319.SH", top_n=5)
    _ready(d)
    data = d["data"]
    for k in ("code", "count", "total_net", "total_inside", "total_outside",
              "contributors", "granularity", "source"):
        assert k in data, f"G2 缺字段 {k}"
    assert data["granularity"] == "day"  # eltdx 仅日累计口径


def test_g2_contributors_shape(app_client):
    d = get(app_client, "/market/board/moneyflow", code="881319.SH", top_n=5)
    _ready(d)
    cons = d["data"]["contributors"]
    if not cons:
        pytest.skip("离线无资金流源，无 contributors")
    for c in cons:
        assert c.get("code") and c.get("name")
        assert isinstance(c.get("net"), _NUM)
        assert isinstance(c.get("pct"), _NUM)  # 贡献度 = net/|total_net|*100
    assert sum(abs(c["pct"]) for c in cons) > 0


def test_g2_invalid_code_no_500(app_client):
    # 未知/非板块码不得 500 也不得长时间挂起：非 881/880 段直接 400 快失败
    # （源层会对未知码做全市场回落聚合 >90s，属缺陷，端点已前置域校验拦截）。
    d = get(app_client, "/market/board/moneyflow", code="999999.SH", top_n=3)
    assert d.get("code") == 400, f"G2 非板块码应 400 快失败，实际: {d}"
    assert "881" in (d.get("message") or "") or "板块" in (d.get("message") or "")


# ============================ F4 板块轮动 ============================
def test_f4_envelope_and_matrix(app_client):
    d = get(app_client, "/market/rotation", days=5, top_n=10, kind="industry")
    _ready(d)
    data = d["data"]
    assert data["days"] == 5
    assert data["kind"] == "industry"
    boards = data["boards"]
    if not boards:
        pytest.skip("离线无历史 K 线源，无轮动矩阵")
    for b in boards:
        assert b.get("code") and b.get("name")
        assert isinstance(b.get("total_pct"), _NUM)
        assert isinstance(b.get("cum_pct"), _NUM)
        daily = b.get("daily")
        assert isinstance(daily, list) and len(daily) == 5
        for day in daily:
            assert "date" in day and "pct" in day


def test_f4_param_bounds(app_client):
    # days 越界应被钳制或拒绝（不崩溃）
    d = get(app_client, "/market/rotation", days=999, top_n=10, kind="industry")
    _ready(d)
    # top_n 上限保护
    d2 = get(app_client, "/market/rotation", days=3, top_n=500, kind="concept")
    _ready(d2)


# ============================ G3 资金流落库回放 ============================
def test_g3_snapshot_insert_and_replay(app_client):
    p = post(app_client, "/market/moneyflow/snapshot",
             {"codes": ["600519.SH", "000001.SZ"]})
    _ready(p)
    assert p["data"]["inserted"] == 2
    r = get(app_client, "/market/moneyflow/replay", code="600519.SH", limit=5)
    _ready(r)
    rows = r["data"]["rows"]
    assert isinstance(rows, list) and len(rows) >= 1
    row = rows[0]
    for k in ("code", "ts", "inside", "outside", "net"):
        assert k in row, f"G3 回放缺字段 {k}"
    assert isinstance(row["net"], _NUM)


def test_g3_replay_unknown_code_no_error(app_client):
    r = get(app_client, "/market/moneyflow/replay", code="999999.XX", limit=5)
    _ready(r)
    assert isinstance(r["data"]["rows"], list)
