"""「裸 except: pass」**只降不升**护栏。

## 为什么钉这个数

``except Exception: pass`` 是最容易埋雷的一种写法：它把「出错了」和「没出错」
渲染成完全一样的结果 —— 不报错、不记日志、不改变流程。出事时唯一的线索被抹掉了。

一次性改掉全部 151 处不现实（其中一部分确实是刻意的），但**新增**一处是完全
可以避免的。所以这里钉一个水位线：

- **新增**一处 ⇒ 立刻失败（逼调用方改用 :func:`core.errors.swallow` 并写出理由）；
- **删掉**一处 ⇒ 通过（水位线只降不升，鼓励收敛）。

⚠️ 基线是**快照**，不是目标：把水位线调低才算改进，调高必须给出理由并改这里。
"""
from __future__ import annotations

import re
from pathlib import Path

#: 允许出现 `except ...: pass` 的**最多**处数（2026-09-19 实测快照）。
MAX_BARE_EXCEPT_PASS = 151

#: 扫描范围（与 ci_reconcile 的口径一致：排除 runtimes / dist / build）
ROOTS = ("app", "core", "datasource", "engines", "gateway", "tools", "xtquant_client")

#: 匹配 ``except ...:`` 后面**紧跟** pass（允许中间夹一行注释 / noqa）
_PATTERN = re.compile(r"except[^\n]*:\s*(?:#[^\n]*\n\s*)*pass\b")


def _scan(root: Path) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for name in ROOTS:
        base = root / name
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except OSError:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if _PATTERN.search(line):
                    out.append((str(p), i, line.strip()))
            # 跨行形态：except 与 pass 不在同一行
            for m in _PATTERN.finditer(txt):
                seg = m.group(0)
                if "\n" in seg:
                    out.append((str(p), txt[: m.start()].count("\n") + 1,
                                seg.replace("\n", " ⏎ ").strip()))
    return out


def test_bare_except_pass_never_grows():
    root = Path(__file__).resolve().parent.parent
    hits = _scan(root)
    # 跨行形态会被上面两种方式各记一次，去重
    unique = sorted(set(hits))
    assert len(unique) <= MAX_BARE_EXCEPT_PASS, (
        f"裸 `except ...: pass` 从 {MAX_BARE_EXCEPT_PASS} 处增加到 {len(unique)} 处。"
        f"新增的必须改用 core.errors.swallow(exc, why=...) 并写出可忽略的理由"
        f"（静默失败是本项目最难排查的一类问题）。\n"
        + "\n".join(f"  {f}:{ln}  {src}" for f, ln, src in unique[:15])
    )


def test_swallow_records_reason():
    """``swallow`` 必须**留下痕迹** —— 否则它与裸 pass 没有区别。"""
    import logging

    from core.errors import swallow

    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger("test.swallow")
    log.setLevel(logging.DEBUG)
    h = _Handler()
    log.addHandler(h)
    try:
        swallow(ValueError("boom"), why="单元测试：仅验证会记日志", logger=log,
                level=logging.DEBUG)
    finally:
        log.removeHandler(h)
    assert records, "swallow 没有记录任何日志 ⇒ 与裸 pass 无异"
    assert "单元测试" in records[0], records
    assert "boom" in records[0], "异常信息必须进日志，否则排查时无从下手"


def test_swallow_never_raises():
    """日志本身炸了也绝不能把调用方带崩 —— 兜底的东西不能成为新的故障源。"""
    from core.errors import swallow

    class _Boom:
        def log(self, *a, **k):
            raise RuntimeError("logger broken")

        def isEnabledFor(self, *_a):
            return True

    swallow(ValueError("x"), why="测试", logger=_Boom())  # type: ignore[arg-type]
