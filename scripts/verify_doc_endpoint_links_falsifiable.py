"""文档端点断链门禁的可证伪性验证（2026-10-09 新增门禁的配套守卫）。

`check_doc_endpoint_links.py` 属于**放松就静默变绿**的类型：一旦有人把契约比对改成
「文档里随便写都算过」，或者把扫描范围悄悄收成空，它就会永远绿着，而文档里继续写
不存在的接口（BROKER_ONBOARDING 的 `profiles/hotplug` 就是这么活下来的）。
所以必须能用「制造违规 ⇒ 变红」证明它真的在起作用。

三例（每例后字节级还原）：
  A. 在 docs/ 下放一个写有 **2 个契约里不存在** 的端点的 md ⇒ rc=1 且两条都被点名。
  B. 删掉它 ⇒ rc=0（对照组：证明 A 的红不是因为环境本来就红）。
  C. 放一个只写 **省略/分支简写**（`...` 与 `{check|pull}`）的 md ⇒ rc 必须**保持 0**，
     且扫描到的引用数增加 —— 证明「跳过简写」不是靠「干脆什么都不扫」实现的。

用法：python scripts/verify_doc_endpoint_links_falsifiable.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_doc_endpoint_links.py"
TMP = ROOT / "docs" / "_falsifiable_tmp.md"
PY = sys.executable

FAKE = """# 可证伪自检用临时文件（会自动删除）

GET /api/v1/definitely/not/here
POST /api/v1/also/fake
"""

SHORTHAND = """# 可证伪自检用临时文件（会自动删除）

省略写法必须被跳过（不是真实路径）：
GET /api/v1/...
GET /api/v1/qmt-agent/distribute/{check|pull}
"""


def run() -> tuple:
    r = subprocess.run([str(PY), str(GATE)], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _refs(out: str) -> int:
    """从输出里解析「文档声明的端点引用 N 条」。"""
    for line in out.splitlines():
        if "文档声明的端点引用" in line:
            digits = "".join(ch for ch in line.split("；")[-1] if ch.isdigit())
            if digits:
                return int(digits)
    return -1


def main() -> int:
    fails = []
    base_rc, base_out = run()
    base_refs = _refs(base_out)
    print("[基线] rc=%d 引用数=%d" % (base_rc, base_refs))
    if base_rc != 0:
        print("[FAIL] 基线就不是绿的，无法做对照 —— 先修环境")
        return 1

    # ---- A. 造假端点 ⇒ 必须变红且点名两条 ----
    try:
        TMP.write_text(FAKE, encoding="utf-8")
        rc, out = run()
        ok = rc == 1 and "definitely/not/here" in out and "also/fake" in out
        print("[A] 造假端点 -> rc=%d | 两条都被点名=%s" % (rc, ok))
        if not ok:
            fails.append("A：造假端点没有被抓出（门禁是假绿的）")
    finally:
        if TMP.exists():
            TMP.unlink()

    # ---- B. 还原 ⇒ 必须回绿 ----
    rc_b, out_b = run()
    ok_b = rc_b == 0
    print("[B] 还原 -> rc=%d | 回绿=%s" % (rc_b, ok_b))
    if not ok_b:
        fails.append("B：删除临时文件后仍不回绿（A 的红可能来自环境）")

    # ---- C. 省略/分支简写 ⇒ 必须保持绿，但引用计数确实增长 ----
    try:
        TMP.write_text(SHORTHAND, encoding="utf-8")
        rc_c, out_c = run()
        refs_c = _refs(out_c)
        # SHORTHAND 里两条都该被跳过 ⇒ 引用数应等于基线
        ok_c = rc_c == 0 and refs_c == base_refs
        print("[C] 省略/分支简写 -> rc=%d 引用数=%d（基线 %d）| 正确跳过=%s"
              % (rc_c, refs_c, base_refs, ok_c))
        if not ok_c:
            fails.append("C：简写没有被正确跳过（rc=%d, refs=%s vs %s）"
                         % (rc_c, refs_c, base_refs))
        # 反向锚：同一文件里再放一条**真实**端点，引用数必须 +1 ——
        # 证明「跳过简写」没有顺手把整段扫描也跳掉。
        TMP.write_text(SHORTHAND + "GET /api/v1/capabilities\n", encoding="utf-8")
        rc_d, out_d = run()
        refs_d = _refs(out_d)
        ok_d = rc_d == 0 and refs_d == base_refs + 1
        print("[C-2] 简写 + 1 条真实端点 -> rc=%d 引用数=%d | 扫描仍在生效=%s"
              % (rc_d, refs_d, ok_d))
        if not ok_d:
            fails.append("C-2：真实端点未被计入（refs=%s，期望 %s）"
                         % (refs_d, base_refs + 1))
    finally:
        if TMP.exists():
            TMP.unlink()

    # ---- 收尾复核 ----
    rc_end, _ = run()
    if rc_end != 0:
        fails.append("收尾：临时文件清理后门禁仍非绿")

    print()
    if fails:
        print("[FAIL] 可证伪守卫未通过：")
        for f in fails:
            print("   -", f)
        return 1
    print("[OK] 三例全部可证伪：造假必红、还原必绿、简写跳过但扫描仍在生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
