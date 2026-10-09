#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文档端点断链门禁：docs/ 里声明的 REST 端点必须能在契约里查到。

为什么需要它（2026-10-09 真缺陷）：
    ``docs/BROKER_ONBOARDING.md`` 长期写着「POST /api/v1/brokers/profiles/hotplug」，
    而这个端点**从未存在** —— 热插拔实际是 ``POST /api/v1/brokers`` 的内部自动行为。
    读者按文档去调只会得到 404，且因为「文档不在 CI 上」，这条断链挂了很久没人发现。

判定口径（宁可漏报，不可误报）：
    - 只扫 ``docs/*.md``（含子目录，**排除** ``archive/`` 与 ``release-notes/``：
      那是冻结的历史事实，不该用今天的契约判真伪）；
    - 只认形如 ``<METHOD> /api/v1/<path>`` 的完整写法（METHOD 大写）；
    - 路径里出现 ``...`` / ``|`` / 未闭合的 ``{`` 一律跳过（那是省略号与
      ``{check|pull}`` 之类的**书写简写**，不是真实路径）；
    - 契约以 ``backend/tests/contracts/rest_endpoints.json`` 为唯一真源。

退出码：0 = 无断链；1 = 存在断链（并逐条打印 文件 / 行号 / 端点）。
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTRACT = os.path.join(ROOT, "backend", "tests", "contracts", "rest_endpoints.json")
DOCS = os.path.join(ROOT, "docs")

#: 冻结历史源，不参与判定（理由同 audit_doc_links.py）
SKIP_DIRS = ("archive", "release-notes")

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
#: 路径字符不含空格；结尾允许 } 或普通字符
PATH_RE = r"/api/v1/[A-Za-z0-9_/{}.\-]*"
LINE_RE = re.compile(r"\b(" + "|".join(METHODS) + r")\s+(" + PATH_RE + r")")


def _is_ellipsis(path: str) -> bool:
    """省略号 / 分支简写 / 未闭合花括号 —— 都不是真实路径，跳过。"""
    if "..." in path or "|" in path:
        return True
    return path.count("{") != path.count("}")


def _known_endpoints() -> set:
    with open(CONTRACT, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return set(data.keys())
    out = set()
    for it in data or []:
        if isinstance(it, dict) and it.get("method") and it.get("path"):
            out.add("%s %s" % (str(it["method"]).upper(), it["path"]))
    return out


def main() -> int:
    if not os.path.isfile(CONTRACT):
        print("[FAIL] 契约文件不存在：%s" % CONTRACT)
        return 1
    known = _known_endpoints()

    broken: list = []
    checked = 0
    for dp, dns, fns in os.walk(DOCS):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in sorted(fns):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(dp, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            for lineno, line in enumerate(lines, 1):
                for m in LINE_RE.finditer(line):
                    p = m.group(2)
                    if _is_ellipsis(p):
                        continue
                    checked += 1
                    key = "%s %s" % (m.group(1), p)
                    if key not in known:
                        broken.append((os.path.relpath(path, ROOT), lineno, key))

    print("契约端点 %d；文档声明的端点引用 %d 条" % (len(known), checked))
    if not broken:
        print("[OK] docs/ 里声明的 REST 端点全部能在契约里查到（0 断链）")
        return 0
    print("[FAIL] 文档声明了 %d 个契约里不存在的端点：" % len(broken))
    for rel, lineno, key in broken:
        print("   %s:%s  %s" % (rel, lineno, key))
    print("\n处置：要么补实现并同步契约（`python backend/scripts/gen_contracts.py`），"
          "要么改文档 —— 文档写了不存在的接口，比不写更坑。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
