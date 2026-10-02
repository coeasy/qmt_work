# -*- coding: utf-8 -*-
"""P0 Bundle 完整性护栏（R17 引入）：AST 语法校验 + 污染签名检测。

R17 真实事故复盘：`QMT_WORK_AGENT.py` 头部被拼了 18 行另一支 CCI 策略
（`import pandas/numpy/talib` + `init/handlebar` stub），Python 解释到
line 53 撞 docstring 边界时 `IndentationError`，用户直到点「运行」才暴雷。
本测试锁住两类护栏：
  1. `check_bundle_syntax`     —— AST 解析能否通过
  2. `check_bundle_pollution`  —— 前 N 行是否出现非标准库 import
"""
from __future__ import print_function

import io
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from qmt_agent_verify import (  # noqa: E402
    POLLUTION_IMPORTS,
    bundle_health,
    check_bundle_pollution,
    check_bundle_syntax,
)


@pytest.fixture()
def tmp_file(tmp_path):
    def _write(name, text, encoding="utf-8"):
        p = tmp_path / name
        with io.open(str(p), "w", encoding=encoding) as fh:
            fh.write(text)
        return str(p)
    return _write


# ---------------------------------------------------------------------------
# 语法校验
# ---------------------------------------------------------------------------
def test_syntax_clean_passes(tmp_file):
    p = tmp_file("clean.py", "def f():\n    return 1\n")
    out = check_bundle_syntax(p)
    assert out["ok"] is True
    assert out["encoding"] == "utf-8"
    assert out["line_count"] >= 2


def test_syntax_indentation_error_flagged(tmp_file):
    # R17 真实事故：docstring 边界处缩进错乱
    p = tmp_file("broken.py",
                 "def f():\n    return 1\n\nif True:\nprint('x')\n")
    out = check_bundle_syntax(p)
    assert out["ok"] is False
    assert "SyntaxError" in (out["error"] or "")
    assert "line" in out["error"]


def test_syntax_gbk_encoding_ok(tmp_file):
    """QMT 的 bundle 是 GB18030 编码；语法校验要能吃。"""
    p = tmp_file("gbk.py", "# -*- coding: gb18030 -*-\n# 中文注释\nx = 1\n",
                 encoding="gb18030")
    out = check_bundle_syntax(p)
    assert out["ok"] is True
    assert out["encoding"] in ("gb18030", "gbk")


# ---------------------------------------------------------------------------
# 污染签名检测
# ---------------------------------------------------------------------------
def test_pollution_pandas_numpy_talib_detected(tmp_file):
    """R17 真实事故的前缀污染：pandas/numpy/talib 出现在前 20 行。"""
    p = tmp_file("polluted.py",
                 '#encoding:gbk\n'
                 '"""\n一些策略描述\n"""\n'
                 'import pandas as pd\n'
                 'import numpy as np\n'
                 'import talib\n\n'
                 'def init(ContextInfo):\n    pass\n\n'
                 'def handlebar(ContextInfo):\n    pass\n')
    out = check_bundle_pollution(p)
    assert out["ok"] is False
    assert "pandas@L5" in out["imports_found"]
    assert "numpy@L6" in out["imports_found"]
    assert "talib@L7" in out["imports_found"]
    assert out["first_import_line"] == 5


def test_pollution_clean_bundle_no_hits(tmp_file):
    p = tmp_file("clean.py",
                 "# -*- coding: utf-8 -*-\n"
                 '"""qmt_work agent"""\n'
                 "import json\n"
                 "import os\n"
                 "import sys\n"
                 "import time\n")
    out = check_bundle_pollution(p)
    assert out["ok"] is True
    assert out["imports_found"] == []


def test_pollution_ignores_comments_and_stdlib(tmp_file):
    """注释里的 'pandas' 不算污染；标准库 import 不算。"""
    p = tmp_file("tricky.py",
                 "# 曾经有人问能不能用 pandas\n"
                 "import json\n"
                 "import os\n"
                 "print('hello')\n")
    out = check_bundle_pollution(p)
    assert out["ok"] is True


def test_pollution_list_contains_really_bad_things():
    """护栏本身要有明确的检测范围声明。"""
    for name in ("pandas", "numpy", "talib", "sklearn", "torch"):
        assert name in POLLUTION_IMPORTS


# ---------------------------------------------------------------------------
# 聚合接口
# ---------------------------------------------------------------------------
def test_bundle_health_aggregates(tmp_file):
    p = tmp_file("polluted.py",
                 "import pandas as pd\n"
                 "def f():\n    pass\n")
    out = bundle_health(p)
    assert out["ok"] is False
    assert out["syntax_ok"] is True        # 语法本身是对的
    assert out["pollution_ok"] is False    # 但被污染
    assert out["pollution_hits"]


def test_bundle_health_missing_file():
    missing = os.path.join(tempfile.gettempdir(), "no_such_bundle_9999.py")
    assert not os.path.exists(missing)
    out = bundle_health(missing)
    assert out["ok"] is False
    assert out["syntax_ok"] is False
    assert out["pollution_ok"] is True  # 读不到就没测
