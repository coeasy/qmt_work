"""异常兜底的单一入口。

## 为什么要有这个模块

项目里有上百处 ``except Exception: pass``。它们的**本意**各不相同：

- 一类是**刻意的**：观测增强项（缓存统计、通知、审计）失败绝不能拖垮主流程；
- 另一类只是**当时懒得处理**，真出错时连一行日志都没有。

问题在于：这两类在代码里长得一模一样。后果是

1. 出事时**没有任何线索**（日志里一行都没有，只能靠猜）；
2. 静态审查分不出「刻意忽略」和「吞掉真错误」；
3. 后来者不敢删 —— 不知道删掉会发生什么。

``swallow`` 把「刻意吞掉」变成一个**显式、带理由、会留痕**的动作：

.. code-block:: python

    try:
        metrics.record(...)
    except Exception as exc:                      # noqa: BLE001
        swallow(exc, why="指标统计失败不影响主流程")

调用方必须写出**为什么可以忽略**，运行时按 DEBUG 记一条（默认不刷屏，但真要查
的时候 `logging` 调到 DEBUG 就能看到）。

⚠️ 本模块**不**提供「静默版」：没有理由的吞掉一律不许存在。历史存量由
``tests/test_no_bare_except_pass.py`` 的「只降不升」护栏看着，逐步收敛。
"""
from __future__ import annotations

import logging
from typing import Optional

__all__ = ["swallow", "swallow_async"]

_log = logging.getLogger("qmt_work.swallow")


def swallow(exc: BaseException, *, why: str,
            logger: Optional[logging.Logger] = None,
            level: int = logging.DEBUG) -> None:
    """吞掉一个**已判断为可忽略**的异常，并留下可查的痕迹。

    ``why`` 是必填关键字参数 —— 这不是形式主义：写下理由的那几秒，正是区分
    「真的可以忽略」和「其实该往上抛」的时刻。历史上绝大多数静默失败都源于
    「当时觉得无所谓」。

    默认 DEBUG 级：被刻意忽略的异常不该污染正常日志，但排查时调高级别即可全见。
    """
    log = logger or _log
    try:
        log.log(level, "忽略异常（%s）：%s: %s", why, type(exc).__name__, exc)
    except Exception:  # noqa: BLE001 日志本身失败绝不能把调用方带崩
        pass


def swallow_async(exc: BaseException, *, why: str,
                  logger: Optional[logging.Logger] = None) -> None:
    """:func:`swallow` 的别名，用于异步回调里表明「这里是有意不 await 也不抛」。

    语义与 :func:`swallow` 完全一致，单独命名只是为了让读者在 ``asyncio`` 回调里
    一眼看出这是**决策**而不是漏写。
    """
    swallow(exc, why=why, logger=logger)
