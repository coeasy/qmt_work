import sys, traceback
sys.path.insert(0, r"p:\github_public\qmt_work\backend")

def section(name):
    print(f"\n=== {name} ===")

reg = {"broker", "eltdx", "baostock", "akshare", "pytdx", "tencent", "sina"}

try:
    import datasource.providers as p
    section("providers import + 契约顺序")
    # D-J §J.6：默认链严格契约（不受本机安装了哪些依赖影响）
    assert p.DEFAULT_CAPABILITY_CHAINS["kline_qfq"][:4] == ("broker", "eltdx", "baostock", "akshare"), \
        p.DEFAULT_CAPABILITY_CHAINS["kline_qfq"]
    names = [d.id for d in p.PROVIDER_CATALOG]
    assert "broker" in names and "akshare" in names, names
    assert p.PROVIDER_CATALOG[1].commercial_ok is False, "eltdx 必须 commercial_ok=False"
    # describe 暴露 commercial_ok
    desc = {d["provider"]: d for d in p.provider_catalog.describe()}
    assert desc["eltdx"]["commercial_ok"] is False
    print("catalog/contract OK")

    # 链求值：返回必须是默认顺序的子序列（保持降级顺序），且剔除未安装依赖
    ch = p.provider_catalog.resolve_chain("kline_qfq", commercial_mode=False, registered=reg)
    default = list(p.DEFAULT_CAPABILITY_CHAINS["kline_qfq"])
    # 验证顺序一致：ch 应是 default 过滤后的子序列
    it = iter(default)
    for c in ch:
        while True:
            nxt = next(it, None)
            if nxt == c:
                break
            # 跳过 default 中不在 ch 的项（被依赖/许可证过滤）
            assert nxt is not None
    print("resolve_chain order OK:", ch)
    # commercial 模式必须跳过 eltdx
    ch2 = p.provider_catalog.resolve_chain("kline_qfq", commercial_mode=True, registered=reg)
    assert "eltdx" not in ch2, ch2
    print("commercial skips eltdx OK:", ch2)
    print("PROVIDERS OK")
except Exception:
    traceback.print_exc(); sys.exit(1)

try:
    import app.screener.source_policy as sp
    section("source_policy")
    assert sp.parse_source_policy("auto") == sp.SourcePolicy.AUTO
    assert sp.parse_source_policy("explicit:akshare") == sp.SourcePolicy.EXPLICIT
    assert sp.explicit_provider_of("explicit:akshare") == "akshare"
    assert sp.parse_source_policy("garbage") == sp.SourcePolicy.AUTO  # 非法回退 auto
    # QMT_ONLY 无连接 → 空链（调用方据此 503）
    r = sp.resolve_policy("qmt_only", "kline_qfq", registered=reg, qmt_connected=False)
    assert r.chain == (), r.chain
    # QMT_ONLY 有连接 → 仅 broker
    r = sp.resolve_policy("qmt_only", "kline_qfq", registered=reg, qmt_connected=True)
    assert r.chain == ("broker",), r.chain
    # AUTO 无 QMT → 剔除 broker，degraded=True
    r = sp.resolve_policy("auto", "kline_qfq", registered=reg, qmt_connected=False)
    assert "broker" not in r.chain, r.chain
    assert r.degraded is True and r.qmt_unavailable_reason == "no_broker_connected"
    print("auto no-qmt chain:", r.chain, "degraded:", r.degraded)
    # EXPLICIT 不降级
    r = sp.resolve_policy("explicit:akshare", "kline_qfq", registered=reg)
    assert r.chain == ("akshare",), r.chain
    print("SOURCE_POLICY OK")
except Exception:
    traceback.print_exc(); sys.exit(1)

try:
    import datasource.optional_sources as o
    section("baostock code format bug (P0-16)")
    assert o._to_baostock_code("600519.SH") == "sh.600519", o._to_baostock_code("600519.SH")
    assert o._to_baostock_code("000001.SZ") == "sz.000001"
    assert o._to_baostock_code("sh.600519") == "sh.600519"
    print("BAOSTOCK OK")
except Exception:
    traceback.print_exc(); sys.exit(1)

print("\nALL PHASE 3/4 LOGIC CHECKS PASSED")
