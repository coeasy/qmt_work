"""P1-3 可证伪性验证：逐个变异 portfolio_engine / factor_backtest，要求对应测试变红。
每例后字节级还原并复核 sha256。

★ M7 必须是**组合回退**（aligned 用原始 klines + T 取最大长度）：单独改 T 是**空操作** ——
  对齐生效后各袖套长度恰好等于交集长度，max(lens)==len(dates)。这正是「变异锚点
  必须落在真正可执行的分支上」的实例。

用法：backend/runtimes/cp311/python.exe -u scripts/verify_portfolio_falsifiable.py
"""
import hashlib, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BE = ROOT / "backend"
PY = BE / "runtimes" / "cp311" / "python.exe"
ENG = BE / "tools" / "portfolio_engine.py"
FBT = BE / "tools" / "factor_backtest.py"

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def run_one(test_name):
    r = subprocess.run([str(PY), "-m", "pytest", f"tests/test_portfolio_engine.py::{test_name}",
                        "-q", "-p", "no:cacheprovider"],
                       cwd=str(BE), capture_output=True, timeout=300,
                       encoding="utf-8", errors="replace",
                       env={"PATH": str(PY.parent), "no_proxy": "127.0.0.1,localhost",
                            "NO_PROXY": "127.0.0.1,localhost", "SYSTEMROOT": "C://Windows",
                            "TEMP": "C://Windows//Temp", "TMP": "C://Windows//Temp"})
    return r.returncode, (r.stdout or "")[-300:]

CRLF = lambda s: s.replace("\n", "\r\n").encode("utf-8")

CASES = [
    ("M1 分钟线被压成一天（去掉 _date_key 时间分支）", ENG, [
        (b'    if "T" in s or ":" in s:', b'    if False:')],
     "test_align_panel_keeps_intraday_distinct"),
    ("M2 交集变并集", ENG, [
        (b"common = set.intersection(*date_sets) if date_sets else set()",
         b"common = set.union(*date_sets) if date_sets else set()")],
     "test_align_panel_uses_intersection_and_reports_excluded"),
    ("M3 去掉 Σw>1 拒绝（悄悄放行杠杆）", ENG, [
        (b"    if s > 1.0 + 1e-9:", b"    if False:")],
     "test_engine_rejects_weight_sum_over_one"),
    ("M4 去掉整手取整", ENG, [
        (b"                target_shares[s] = (round_lot(raw_q, min_lot) if lot_rounding",
         b"                target_shares[s] = (int(raw_q) if lot_rounding")],
     "test_engine_all_trades_are_whole_lots"),
    ("M5 资金不足误报成通用原因", ENG, [
        (b"    if detail and len(poor) == len(detail):", b"    if False:")],
     "test_engine_diagnostics_insufficient_capital"),
    ("M6 rebalance=none 变成每根都调", ENG, [
        (b"            do_reb = False", b"            do_reb = True")],
     "test_engine_rebalance_none_trades_only_on_first_bar"),
    ("M7 组合回退旧袖套接口的日期对齐（按索引加总）", FBT, [
        (CRLF('    aligned = panel["bars"]'), CRLF('    aligned = {s: klines[s] for s in symbols}')),
        (CRLF('    T = len(panel["dates"])'), CRLF("    T = max(lens.values())"))],
     "test_legacy_portfolio_backtest_aligns_dates_not_indices"),
]

orig = {p: p.read_bytes() for p in (ENG, FBT)}
shas = {p: sha(p) for p in (ENG, FBT)}
for p in (ENG, FBT):
    print(f"{p.name}: sha={shas[p][:16]} len={len(orig[p])}")

bad = []
for desc, path, pairs, test in CASES:
    src = orig[path]
    mutated = src
    miss = [old for old, _ in pairs if mutated.count(old) != 1]
    if miss:
        print(f"  [SKIP] {desc}: 锚点命中异常 {[mutated.count(o) for o,_ in pairs]}")
        bad.append(desc); continue
    for old, new in pairs:
        mutated = mutated.replace(old, new, 1)
    path.write_bytes(mutated)
    rc, tail = run_one(test)
    ok = rc != 0
    print(f"  [{'OK ' if ok else 'BAD'}] {desc} → {test} rc={rc} (期望非 0)")
    if not ok:
        print("       ", tail.strip().splitlines()[-2:]); bad.append(desc)
    path.write_bytes(orig[path])
    if sha(path) != shas[path]:
        print(f"  !!! {path.name} 还原不一致"); bad.append(desc + " [还原失败]")

for p in (ENG, FBT):
    print(f"还原 {p.name}: sha={sha(p)[:16]} 一致={sha(p)==shas[p]}")
rc, _ = run_one("test_engine_equity_identity_cash_plus_market_value")
print("还原后基线单测 rc=", rc)
print("\n结果：", "全部通过" if not bad else f"失败 {bad}")
sys.exit(0 if not bad else 1)
