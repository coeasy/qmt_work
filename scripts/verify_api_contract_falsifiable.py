"""P1-5 可证伪性验证：制造漂移，要求 check_api_contract_drift.py 变红；
  并验证「不应报警」的对照（仅改查询串）。每例后字节级还原。

★ 常驻守卫：改动 `check_api_contract_drift.py` 的归一化逻辑后必须重跑 ——
  归一化一旦写松（例如把路径参数统一成 `{param}` 这一步去掉），检查会**静默变绿**。

★ R19 第 3 轮补两例（E/F）：原脚本只覆盖 `http.get/post` 两种写法，而门禁当时
  **看不见** `http.del(...)`（客户端 DELETE 助手叫 del，不是 delete）与
  `http.post<Record<string, unknown>>(...)`（泛型里多一个 `>` 会匹配失败）。
  那是两个真存在的**假绿灯**：9 个 DELETE 端点 + 一批嵌套泛型调用点从未被对账。
  修好后必须能用「把路径改坏 ⇒ 变红」证明它们**确实进入了**对账范围 ——
  否则修的是一个看不见的洞，没人知道补上没有。

用法：backend/runtimes/cp311/python.exe -u scripts/verify_api_contract_falsifiable.py
"""
import hashlib, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "backend" / "runtimes" / "cp311" / "python.exe"
SCRIPT = ROOT / "scripts" / "check_api_contract_drift.py"
API = ROOT / "frontend-next" / "src" / "services" / "api"

ACCOUNT = API / "account.ts"
AUTOMATION = API / "automation.ts"
MARKET = API / "market.ts"


def run():
    r = subprocess.run([str(PY), str(SCRIPT)], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


# (说明, 目标文件, 原字节, 新字节, 期望 rc)
CASES = [
    ("A. 端点改名（/account/status → /account/status-typo）",
     ACCOUNT, b'"/account/status"', b'"/account/status-typo"', 1),
    ("B. 方法错配（http.get< → http.delete< 同一路径）",
     ACCOUNT, b'http.get<', b'http.delete<', 1),
    ("C. 不存在的顶层段（/account/status → /nope/status）",
     ACCOUNT, b'"/account/status"', b'"/nope/status"', 1),
    ("D. 对照：仅改查询串（不应报）",
     ACCOUNT, b'"/account/grid"', b'"/account/grid?limit=5"', 0),
    ("E. R19-3：http.del 别名必须纳入对账（改坏 DELETE 路径）",
     AUTOMATION, b'`/alerts/rules/${rid}`', b'`/alerts/rules-nope/${rid}`', 1),
    ("F. R19-3：嵌套泛型 http.post<Record<string, unknown>> 必须纳入对账",
     AUTOMATION, b'"/limitup/start"', b'"/limitup/start-nope"', 1),
    ("G. R19-3：嵌套泛型 GET（http.get<Record<string, unknown>>）必须纳入对账",
     MARKET, b'"/market/analysis"', b'"/market/analysis-nope"', 1),
]

originals = {}
for _d, path, _o, _n, _e in CASES:
    originals.setdefault(path, (path.read_bytes(), sha(path)))
for path, (raw, h) in originals.items():
    print(f"原始 {path.name} sha256 = {h[:16]}...  ({len(raw)} bytes)")

fails = []
for desc, path, old, new, expect in CASES:
    orig, orig_sha = originals[path]
    if old not in orig:
        print(f"  ! 锚点未命中，跳过：{desc}")
        fails.append(desc)
        continue
    path.write_bytes(orig.replace(old, new, 1))
    rc, out = run()
    ok = (rc == expect)
    print(f"  [{'OK ' if ok else 'BAD'}] {desc} → rc={rc} (期望 {expect})")
    if not ok:
        print("      输出尾部：", out.strip().splitlines()[-3:])
        fails.append(desc)
    # 立即还原
    path.write_bytes(orig)
    if sha(path) != orig_sha:
        print("  !!! 还原失败，字节不一致")
        fails.append(desc + " [还原失败]")

for path, (raw, h) in originals.items():
    print(f"还原后 {path.name} sha256 = {sha(path)[:16]}...  一致={sha(path) == h}")
print(f"结果：{'全部通过' if not fails else '失败 ' + str(fails)}")
sys.exit(0 if not fails else 1)
