# -*- coding: utf-8 -*-
"""P0 Bundle 完整性护栏（R17 引入）：AST 语法校验 + 污染签名检测 + 源码编码（R27）。

R17 真实事故复盘：`QMT_WORK_AGENT.py` 头部被拼了 18 行另一支 CCI 策略
（`import pandas/numpy/talib` + `init/handlebar` stub），Python 解释到
line 53 撞 docstring 边界时 `IndentationError`，用户直到点「运行」才暴雷。

R27 真实事故复盘（2026-10-08）：52 KB 的 bundle 以 `gb18030` 落盘。
**开发机 Python 3.11 编译通过**，QMT 内置 `bin.x64/pythonw.exe`（**Python 3.6.8**）
却报 `SyntaxError: encoding problem: gb18030` → 进程 return code:1、
模型交易里只见「启动即停止」、**一条自检都不落盘**。所以「语法能过」不等于
「QMT 能跑」——编码必须单列成一项独立判定。

本测试锁住三类护栏：
  1. `check_bundle_syntax`     —— AST 解析能否通过
  2. `check_bundle_pollution`  —— 前 N 行是否出现非标准库 import
  3. `check_bundle_encoding`   —— 源码编码是否 QMT 内置 py3.6 能读
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
    QMT_SAFE_ENCODINGS,
    bundle_health,
    check_bundle_encoding,
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


@pytest.fixture()
def qmt_unsafe(tmp_path):
    """一份「开发机能读、QMT 内置 py3.6 读不了」的 gb18030 源文件。"""
    p = tmp_path / "unsafe.py"
    with io.open(str(p), "w", encoding="gb18030", newline="\n") as fh:
        fh.write("#coding:gb18030\n# 中文注释\nx = 1\n")
    return str(p)


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


def test_syntax_gbk_parses_on_dev_python_but_is_flagged(qmt_unsafe):
    """gb18030 的**开发机**语法校验必须过（否则诊断会把根因指向错误方向）。

    ★ 这正是 R27 最难查的地方：语法校验绿灯、文件也在、注册也正常，
      唯一的区别是 QMT 内置解释器是 py3.6。所以这里要断言
      `ok is True` 且 `encoding_qmt_safe is False` **同时成立**。
    """
    out = check_bundle_syntax(qmt_unsafe)
    assert out["ok"] is True
    assert out["encoding"] in ("gbk", "gb18030")
    assert out["encoding_qmt_safe"] is False
    assert "3.6.8" in (out["encoding_note"] or "")


# ---------------------------------------------------------------------------
# 源码编码（R27 · 2026-10-08 事故）
# ---------------------------------------------------------------------------
def test_encoding_utf8_with_cookie_is_safe(tmp_file):
    p = tmp_file("a.py", "# -*- coding: utf-8 -*-\n# 中文\nx = 1\n")
    out = check_bundle_encoding(p)
    assert out["ok"] is True
    assert out["declared"] == "utf-8"
    assert out["read_as"] == "utf-8"
    assert out["note"] is None


def test_encoding_utf8_without_cookie_is_safe(tmp_file):
    """py3.6 默认就是 UTF-8，没 cookie 也没关系。"""
    p = tmp_file("b.py", "# 中文注释\nx = 1\n")
    assert check_bundle_encoding(p)["ok"] is True


def test_encoding_gb18030_cookie_is_unsafe(qmt_unsafe):
    out = check_bundle_encoding(qmt_unsafe)
    assert out["ok"] is False
    assert out["declared"] == "gb18030"
    # 报错必须给**下一步动作**（本项目的判定纪律：不只报错）
    assert "encoding utf-8" in out["note"]


def test_encoding_non_utf8_without_cookie_is_unsafe(tmp_path):
    """没有 cookie 但内容不是 UTF-8：py3.6 会按 UTF-8 解码，必然炸。"""
    p = tmp_path / "c.py"
    with io.open(str(p), "w", encoding="gb18030", newline="\n") as fh:
        fh.write("# 中文注释\nx = 1\n")
    out = check_bundle_encoding(str(p))
    assert out["ok"] is False
    assert out["declared"] is None
    assert "UTF-8" in (out["note"] or "")


def test_encoding_missing_file_is_unsafe():
    out = check_bundle_encoding(os.path.join(tempfile.gettempdir(), "nope_7777.py"))
    assert out["ok"] is False


def test_encoding_cookie_detection_stops_at_code_line(tmp_path):
    """cookie 只在前两行生效 —— 第三行的注释不能被误判成声明。"""
    p = tmp_path / "d.py"
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("import os\nimport sys\n# coding: gb18030\n")
    out = check_bundle_encoding(str(p))
    assert out["declared"] is None
    assert out["ok"] is True


def test_encoding_safe_set_is_utf8_family():
    for enc in ("utf-8", "utf8", "ascii", "us-ascii"):
        assert enc in QMT_SAFE_ENCODINGS
    for enc in ("gbk", "gb18030", "utf-16", "latin-1", "cp936"):
        assert enc not in QMT_SAFE_ENCODINGS


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
    assert out["encoding_ok"] is True      # 编码没问题（utf-8 无 cookie）
    assert out["pollution_hits"]


def test_bundle_health_flags_unsafe_encoding_even_when_clean(tmp_file):
    """语法对、无污染，但编码不安全 ⇒ 聚合判定必须红。

    R27 之前这里会一路绿灯，然后在 QMT 里「启动即停止」。
    """
    p = tmp_file("gbk_clean.py", "#coding:gb18030\n# 中文\nx = 1\n",
                 encoding="gb18030")
    out = bundle_health(p)
    assert out["syntax_ok"] is True
    assert out["pollution_ok"] is True
    assert out["encoding_ok"] is False
    assert out["ok"] is False
    assert out["encoding_read_as"] in ("gbk", "gb18030")
    assert "3.6.8" in (out["encoding_note"] or "")


def test_bundle_health_ok_requires_encoding_ok(tmp_file):
    """全绿路径的反向锚：utf-8 干净 bundle 必须三项全过。"""
    p = tmp_file("clean.py",
                 "# -*- coding: utf-8 -*-\nimport json\nimport os\nx = 1\n")
    out = bundle_health(p)
    assert (out["syntax_ok"], out["pollution_ok"], out["encoding_ok"]) == (
        True, True, True)
    assert out["ok"] is True


def test_bundle_health_missing_file():
    missing = os.path.join(tempfile.gettempdir(), "no_such_bundle_9999.py")
    assert not os.path.exists(missing)
    out = bundle_health(missing)
    assert out["ok"] is False
    assert out["syntax_ok"] is False
    assert out["pollution_ok"] is True  # 读不到就没测
    assert out["encoding_ok"] is False  # 读不到 ⇒ 不能宣称安全
