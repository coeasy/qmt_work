"""G11-6 前端样式防回归：组件 className 与 styles.css 差集检测。

组件里出现的 className 若未在 styles.css 定义，该页将完全无样式（用户感知
「界面无法加载」）。本脚本扫描 frontend/src 下 jsx/js 的字面量 className，
与 styles.css 已定义类做差集，缺失即 exit 1（纳入 CI）。

范围：仅静态字面量（className="a b" / className={'a b'}）；模板字符串拼接
（`a ${x ? 'b' : 'c'}`）按拆出的字面量片段解析，避免误报。动态 class 以
真实值在运行期才能确定，此处不追（由 Playwright 冒烟兜底）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根
FRONTEND = ROOT / "frontend"
SRC = FRONTEND / "src"
CSS = SRC / "styles.css"

_LITERAL = re.compile(r'className\s*=\s*"([^"]+)"')
_JSX_EXPR = re.compile(r"className\s*=\s*\{\s*(?:`([^`]+)`|'([^']+)'|\"([^\"]+)\")\s*\}")
# 模板字符串内的 ${...} 动态段按空格切分，仅收集纯字面量 token
_TMPL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_CSS_CLASS = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)")


def _css_classes() -> set:
    text = CSS.read_text(encoding="utf-8")
    # 排除 :root 伪类/数字开头误匹配（以 . 开头的选择器）
    return set(m.group(1) for m in _CSS_CLASS.finditer(text))


def _used_classes() -> dict:      # class -> [file:line]
    used: dict = {}
    for p in sorted(SRC.rglob("*")):
        if p.suffix not in (".jsx", ".js") or "node_modules" in str(p):
            continue
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for m in _LITERAL.finditer(line):
                for tok in m.group(1).split():
                    if tok.startswith("${"):
                        continue
                    used.setdefault(tok, []).append(f"{p.relative_to(SRC)}:{i}")
            for m in _JSX_EXPR.finditer(line):
                raw = m.group(1) or m.group(2) or m.group(3) or ""
                # 先剔除 ${...} 动态段（变量名不是类名），再切字面量 token
                raw = re.sub(r"\$\{[^}]*\}", " ", raw)
                for tok in _TMPL_TOKEN.findall(raw):
                    used.setdefault(tok, []).append(f"{p.relative_to(SRC)}:{i}")
    return used


def main() -> int:
    css = _css_classes()
    used = _used_classes()

    def _resolved(token: str) -> bool:
        """后缀前缀（pane-split- / ws-）→ 任一 CSS 类以该前缀开头即视为已定义；
        动态值 token（k/v/btn…）命中基线豁免。"""
        if token in css or token.startswith("ib-"):
            return True
        if token.endswith("-"):
            return any(c.startswith(token) for c in css)
        return token in _BASELINE_DYNAMIC

    new_missing = {c: loc for c, loc in used.items() if not _resolved(c)}
    baseline_hits = [c for c in used if c in _BASELINE_DYNAMIC or c.endswith("-")]
    if new_missing:
        print(f"[FAIL] {len(new_missing)} 个新增 className 未在 styles.css 定义：")
        for c in sorted(new_missing):
            print(f"  .{c}  <- {new_missing[c][0]}")
        print(f"（基线豁免 {len(baseline_hits)} 个历史遗留/动态 token，新增缺失类阻断 CI）")
        return 1
    print(f"[OK] 扫描 {len(used)} 个 className，styles.css 定义 {len(css)} 个类，"
          f"基线豁免 {len(baseline_hits)}，无新增缺失。")
    return 0


# 历史遗留/动态拼接豁免基线：仅豁免既有类，新增 className 缺失仍阻断（防回归）
_BASELINE_DYNAMIC = {
    "k", "v", "btn", "link", "tab", "t", "ts", "x", "y", "st", "status",
    "trade", "watch", "verify", "triggered", "ws", "src", "source", "tag",
    "tagOf", "testRes", "testResult", "xtquant_found", "xtquant_importable",
    "boards-page", "cmd-label", "dock-watch", "etf-d-chart", "etfs-page",
    "io-trend", "mf-replay", "wb-menu-layout", "pane-split-", "source-", "src-", "ws-",
}


if __name__ == "__main__":
    sys.exit(main())
