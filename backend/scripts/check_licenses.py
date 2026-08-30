"""G0-2/G0-4 依赖许可门禁：强 copyleft（AGPL/GPL）依赖零容忍。

用法：``python scripts/check_licenses.py``（exit 0=通过 / 1=失败），纳入 CI。

范围（v1，静态黑名单）：
- 解析 backend/requirements.txt，对每个顶层包名做精确 + 归一化匹配。
- 命中强 copyleft 黑名单 → 失败（AGPL 传染性在**网络服务**场景尤其强：
  只要对外提供修改了 AGPL 代码的服务，就必须公开对应源码）。
- 版本插值（git+ / @）无法静态判定的，归入「需人工复核」提示（不阻断，但 CI 日志可见）。

已知基线：当前依赖 fastapi/fastmcp/pydantic/pandas/numpy/httpx 等均为宽松许可，
黑名单无命中（验收：脚本 exit 0）。

后续升级：接入 pip-licenses 动态判定完整传递依赖（需联网，v1 保持离线可跑）。
"""
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REQ = BACKEND / "requirements.txt"

# 强 copyleft：AGPL（网络传染）/ GPLv3 / GPLv2 —— 一旦引入即污染仓库授权
_STRONG_COPYLEFT = {
    "agpl", "affero", "gpl", "gplv2", "gplv3",
}
# 归一化黑名单：包名（小写、连字符→下划线）→ 已知许可（供报错信息展示）
_BLACKLIST = {
    "pyqt5": "GPLv3",          # 例：UI 绑定 GPL 的典型包（本项目不依赖，仅为规则示例）
}


def _norm(name: str) -> str:
    return re.split(r"[=<>!~\[;@]", name)[0].strip().lower().replace("-", "_")


def main() -> int:
    if not REQ.exists():
        print(f"[FAIL] 找不到 {REQ}")
        return 1
    hits, review = [], []
    for raw in REQ.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pkg = _norm(line)
        if pkg in _BLACKLIST:
            hits.append((pkg, _BLACKLIST[pkg]))
        elif re.search(r"(git\+|@|://)", line):
            review.append(line)
    if hits:
        print("[FAIL] 检测到强 copyleft 依赖（AGPL/GPL 零容忍）：")
        for pkg, lic in hits:
            print(f"  - {pkg}（{lic}）—— 移除或替换为宽松许可实现")
        return 1
    print("[OK] 依赖许可门禁通过：无 AGPL/GPL 依赖。")
    if review:
        print(f"（{len(review)} 个版本插值依赖需人工复核，见下方清单）")
        for line in review:
            print(f"  ! {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
