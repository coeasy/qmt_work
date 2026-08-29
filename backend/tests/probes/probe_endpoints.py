"""新端点冒烟：指数 / 板块 / 成分股 / 板块K线 / ETF / 资金流 / 股本涨跌停。

需后端已在 http://127.0.0.1:21120 运行。用法：
    python tests/probe_endpoints.py [base_url]
"""
import json
import sys
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:21120") + "/api/v1"
PASS, FAIL = [], []


def get(path, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    # 绕过系统代理（本机直连）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK' if cond else 'FAIL':<4}] {name}{(' — ' + detail) if detail else ''}")


def main():
    print("=" * 76)
    print("E1) GET /market/indices")
    try:
        d = get("/market/indices")
        items = (d.get("data") or {}).get("items") or []
        codes = (d.get("data") or {}).get("codes") or []
        print(f"  code={d.get('code')} 返回 {len(items)} 只")
        for c, q in zip(codes, items):
            if q:
                print(f"    {c}  {str(q.get('name')):<10} last={q.get('last'):<10} "
                      f"pct={q.get('change_pct')}")
        check("E1 指数全部取到", len(items) == len(codes) and all(items))
        check("E1 指数有中文名",
              all(q.get("name") and q.get("name") != c for c, q in zip(codes, items)))
        check("E1 指数有涨跌幅", all(q.get("change_pct") is not None for q in items))
    except Exception as e:
        check("E1 指数端点", False, f"{type(e).__name__}: {e}")

    print()
    print("F1) GET /market/boards")
    try:
        d = get("/market/boards", kind="industry", limit=6)
        data = d.get("data") or {}
        items = data.get("items") or []
        print(f"  code={d.get('code')} kind={data.get('kind')} 共 {len(items)} 行 "
              f"source={data.get('source')}")
        for it in items[:6]:
            print(f"    {it['code']:<12}{str(it.get('name')):<12}"
                  f"{str(it.get('last')):>10}  {str(it.get('change_pct')):>7}%")
        check("F1 行业板块榜非空", len(items) > 0)
        check("F1 板块有涨跌幅", all(i.get("change_pct") is not None for i in items))
    except Exception as e:
        check("F1 板块榜", False, f"{type(e).__name__}: {e}")

    print()
    print("F1b) GET /market/boards?kind=concept")
    try:
        d = get("/market/boards", kind="concept", limit=5)
        items = (d.get("data") or {}).get("items") or []
        for it in items[:5]:
            print(f"    {it['code']:<12}{str(it.get('name')):<12}"
                  f"{str(it.get('last')):>10}  {str(it.get('change_pct')):>7}%")
        check("F1b 概念板块榜非空", len(items) > 0)
        check("F1b 概念榜已剔除统计类",
              not any("涨跌家数" in (i.get("name") or "") for i in items))
    except Exception as e:
        check("F1b 概念板块榜", False, f"{type(e).__name__}: {e}")

    print()
    print("F2) GET /market/board/constituents")
    bcode = None
    try:
        d = get("/market/boards", kind="industry", limit=1)
        items = (d.get("data") or {}).get("items") or []
        bcode = items[0]["code"] if items else None
        if bcode:
            d2 = get("/market/board/constituents", code=bcode, limit=6)
            data = d2.get("data") or {}
            subs = data.get("items") or []
            print(f"  板块 {bcode} 总数={data.get('total')} 取回 {len(subs)}")
            for it in subs[:6]:
                print(f"    {it['code']:<12}{str(it.get('name')):<12}"
                      f"{str(it.get('last')):>9}  {str(it.get('change_pct')):>7}%")
            check("F2 成分股非空", len(subs) > 0)
            check("F2 成分股有代码与名称",
                  all(s.get("code") and s.get("name") for s in subs))
    except Exception as e:
        check("F2 成分股", False, f"{type(e).__name__}: {e}")

    print()
    print("F3) GET /market/board/kline")
    try:
        if bcode:
            d = get("/market/board/kline", code=bcode, period="1d", count=5)
            bars = (d.get("data") or {}).get("bars") or []
            print(f"  板块 {bcode} 返回 {len(bars)} 根")
            for b in bars[-3:]:
                print(f"    {b['time']} O={b['open']} H={b['high']} "
                      f"L={b['low']} C={b['close']}")
            check("F3 板块 K 线非空", len(bars) > 0)
    except Exception as e:
        check("F3 板块 K 线", False, f"{type(e).__name__}: {e}")

    print()
    print("H1) GET /market/etfs")
    try:
        d = get("/market/etfs", limit=12)
        data = d.get("data") or {}
        items = data.get("items") or []
        print(f"  count 字段={data.get('count')} 返回 {len(items)} 只")
        for it in items[:12]:
            print(f"    {it['code']:<12}{str(it.get('name'))}")
        check("H1 ETF 清单非空", len(items) > 0)
        check("H1 ETF 有名称", all(i.get("name") for i in items))
    except Exception as e:
        check("H1 ETF 清单", False, f"{type(e).__name__}: {e}")

    print()
    print("P3) GET /market/moneyflow?code=600519.SH")
    try:
        d = get("/market/moneyflow", code="600519.SH")
        data = d.get("data") or {}
        print(f"  内盘={data.get('inside')} 外盘={data.get('outside')} "
              f"净={data.get('net')} 量比={data.get('volume_ratio')} "
              f"力道点数={len(data.get('strength') or [])} est={data.get('est')}")
        check("P3 资金流有内外盘",
              data.get("inside") is not None and data.get("outside") is not None)
        check("P3 资金流非估算", data.get("est") is False)
    except Exception as e:
        check("P3 资金流", False, f"{type(e).__name__}: {e}")

    print()
    print("I1) GET /market/capital?codes=600519.SH,000001.SZ")
    try:
        d = get("/market/capital", codes="600519.SH,000001.SZ")
        data = d.get("data") or {}
        for k, v in (data.get("shares") or {}).items():
            print(f"  股本 {k}: 流通={v.get('circulating_shares')}")
        for k, v in (data.get("limits") or {}).items():
            print(f"  涨跌停 {k}: 昨收={v.get('pre_close')} 涨停={v.get('limit_up')} "
                  f"跌停={v.get('limit_down')} 规则={v.get('limit_rule')}")
        check("I1 流通股本非空", bool(data.get("shares")))
        check("I1 涨跌停价非空", bool(data.get("limits")))
    except Exception as e:
        check("I1 股本/涨跌停", False, f"{type(e).__name__}: {e}")

    print()
    print("A1) GET /market/board/kline?period=1q 应显式报错（不得静默给日线）")
    try:
        d = get("/market/board/kline", code=bcode or "881057.SH", period="1q", count=5)
        check("A1 季线显式报错", d.get("code") != 0, f"code={d.get('code')} msg={d.get('msg')}")
    except Exception as e:
        check("A1 季线契约", False, f"{type(e).__name__}: {e}")

    print()
    print("E3) GET /market/overview （市场广度 + 指数快照 + 宽度趋势）")
    try:
        d = get("/market/overview")
        data = d.get("data") or {}
        breadth = data.get("breadth") or []
        indices = data.get("indices") or []
        print(f"  code={d.get('code')} 广度项={len(breadth)} 指数={len(indices)} "
              f"宽度趋势点={len((data.get('breadth_trend') or {}).get('series') or [])} "
              f"两市成交额={data.get('two_city_turnover')} source={data.get('source')}")
        check("E3 广度家数真实（metric=count/unit=家）",
              all(b.get("metric") == "count" and b.get("unit") == "家" for b in breadth))
        check("E3 指数快照有 last/涨跌幅",
              all(i.get("last") is not None and i.get("change_pct") is not None for i in indices))
        check("E3 两市成交额缺失显 None（不造假）", data.get("two_city_turnover") is None)
    except Exception as e:
        check("E3 市场概览", False, f"{type(e).__name__}: {e}")

    print()
    print("G2) GET /market/board/moneyflow（板块资金流聚合）")
    try:
        d = get("/market/board/moneyflow", code="881319.SH", top_n=5)
        data = d.get("data") or {}
        cons = data.get("contributors") or []
        print(f"  code={d.get('code')} 成分={data.get('count')} "
              f"净合计={data.get('total_net')} 粒度={data.get('granularity')} "
              f"贡献度top1={cons[0] if cons else None}")
        check("G2 聚合字段齐全", all(k in data for k in
              ("count", "total_net", "total_inside", "total_outside", "contributors")))
        check("G2 仅日累计口径", data.get("granularity") == "day")
        check("G2 贡献度含 code/name/net/pct",
              all(c.get("code") and c.get("name") and c.get("net") is not None
                  for c in cons))
    except Exception as e:
        check("G2 板块资金流", False, f"{type(e).__name__}: {e}")

    print()
    print("F4) GET /market/rotation（板块轮动矩阵）")
    try:
        d = get("/market/rotation", days=5, top_n=10, kind="industry")
        data = d.get("data") or {}
        boards = data.get("boards") or []
        print(f"  code={d.get('code')} days={data.get('days')} kind={data.get('kind')} "
              f"板块数={len(boards)}")
        for b in boards[:3]:
            print(f"    {b['code']:<12}{b.get('name'):<10} 累计={b.get('cum_pct')}%")
        check("F4 矩阵维度=days", all(len(b.get("daily") or []) == 5 for b in boards))
        check("F4 每日含 date/pct", all(
            all("date" in x and "pct" in x for x in (b.get("daily") or [])) for b in boards))
    except Exception as e:
        check("F4 板块轮动", False, f"{type(e).__name__}: {e}")

    print()
    print("G3) POST /market/moneyflow/snapshot + GET /market/moneyflow/replay（落库回放）")
    try:
        p = post("/market/moneyflow/snapshot", {"codes": ["600519.SH", "000001.SZ"]})
        ins = (p.get("data") or {}).get("inserted")
        print(f"  快照写入 inserted={ins}")
        check("G3 快照落库成功", p.get("code") == 0 and ins == 2)
        d = get("/market/moneyflow/replay", code="600519.SH", limit=5)
        rows = (d.get("data") or {}).get("rows") or []
        print(f"  回放 code=600519.SH rows={len(rows)} "
              f"首行 net={rows[0].get('net') if rows else None}")
        check("G3 回放字段齐全", all(
            all(k in r for k in ("code", "ts", "inside", "outside", "net")) for r in rows))
    except Exception as e:
        check("G3 资金流回放", False, f"{type(e).__name__}: {e}")

    print()
    print("=" * 76)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项： " + ", ".join(FAIL))
    print("=" * 76)


if __name__ == "__main__":
    main()
