# -*- coding: utf-8 -*-
"""V10 Phase D4：features/ 域拆分迁移脚本（v2）。

- 移动 13 个业务组件到 features/<domain>/（幂等）；
- 全仓重写相对导入（静态 import/export + 动态 import()）；
- 被移动文件自身的导入按「旧目录解析 → 新目录重算相对路径」修正；
- 幂等：重复运行只做重写，不重复移动。
"""
import os
import re

SRC = r"p:/github_public/qmt_work/frontend/src"

MOVES = {
    "components/Trade.jsx": "features/trading/Trade.jsx",
    "components/Algo.jsx": "features/trading/Algo.jsx",
    "components/Rebalance.jsx": "features/trading/Rebalance.jsx",
    "components/ui/OrderTicketModal.jsx": "features/trading/OrderTicketModal.jsx",
    "components/AccountsGrid.jsx": "features/accounts/AccountsGrid.jsx",
    "components/Screen.jsx": "features/research/Screen.jsx",
    "components/Research.jsx": "features/research/Research.jsx",
    "components/Factors.jsx": "features/research/Factors.jsx",
    "components/MarketData.jsx": "features/market/MarketData.jsx",
    "components/QuoteBoard.jsx": "features/market/QuoteBoard.jsx",
    "components/Brokers.jsx": "features/system/Brokers.jsx",
    "components/Settings.jsx": "features/system/Settings.jsx",
    "components/SystemStatus.jsx": "features/system/SystemStatus.jsx",
}

OLD_TO_NEW = {norm_old: norm_new for norm_old, norm_new in
              ((os.path.normpath(os.path.join(SRC, k.replace("/", os.sep))),
                os.path.normpath(os.path.join(SRC, v.replace("/", os.sep)))) for k, v in MOVES.items())}

STATIC_RE = re.compile(
    r'((?:import|export)\s+(?:[\w*\s{},]+\s+from\s+)?["\'])(\.\.?/[^"\']+)(["\'])')
DYNAMIC_RE = re.compile(r'(import\s*\(\s*["\'])(\.\.?/[^"\']+)(["\'])')


def rewrite_spec(m, head, importer_dir):
    spec = m.group(2)
    target = os.path.normpath(os.path.join(importer_dir, spec.replace("/", os.sep)))
    mapped = OLD_TO_NEW.get(target)
    new_abs = mapped if mapped else target
    rel = os.path.relpath(new_abs, importer_dir).replace(os.sep, "/")
    if not rel.startswith("."):
        rel = "./" + rel
    return head + rel + m.group(3)


def rewrite_text(text, resolve_dir, out_dir):
    """resolve_dir = 导入书写时相对的目录；out_dir = 重写后相对的目录。"""
    def _static(m):
        return m.group(1) + __adj(m.group(2), resolve_dir, out_dir) + m.group(3)

    def _dynamic(m):
        return m.group(1) + __adj(m.group(2), resolve_dir, out_dir) + m.group(3)

    text = STATIC_RE.sub(_static, text)
    text = DYNAMIC_RE.sub(_dynamic, text)
    return text


def __adj(spec, resolve_dir, out_dir):
    target = os.path.normpath(os.path.join(resolve_dir, spec.replace("/", os.sep)))
    mapped = OLD_TO_NEW.get(target)
    new_abs = mapped if mapped else target
    rel = os.path.relpath(new_abs, out_dir).replace(os.sep, "/")
    if not rel.startswith("."):
        rel = "./" + rel
    return rel


def main():
    # 映射已建立（绝对路径键）。移动已完成（历史运行），此处直接进入重写：
    # 对每个文件的每条相对导入，若解析出的绝对路径命中 OLD_TO_NEW（即仍指向旧位置），
    # 则重写为新位置的相对路径；已指向新位置的导入幂等跳过。
    touched = 0
    for dirpath, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__")]
        for f in files:
            if not f.endswith((".js", ".jsx", ".mjs", ".ts", ".tsx")):
                continue
            p = os.path.join(dirpath, f)
            text = open(p, encoding="utf-8").read()
            new_text = rewrite_text(text, dirpath, dirpath)
            if new_text != text:
                open(p, "w", encoding="utf-8", newline="").write(new_text)
                touched += 1
                print("rewrote", os.path.relpath(p, SRC).replace(os.sep, "/"))
    print("DONE, rewritten:", touched)


if __name__ == "__main__":
    main()
