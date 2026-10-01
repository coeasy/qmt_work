"""Dialect 实现集合：每个客户端一种「说什么」。"""
from __future__ import annotations

from .base import Dialect, Ops, UnsupportedOp, WRITE_OPS, is_write_op
from .bigqmt_v1 import BigQmtV1
from .ptrade_v1 import PtradeV1
from .xtquant_v1 import XtQuantV1

__all__ = [
    "Dialect", "Ops", "UnsupportedOp", "WRITE_OPS", "is_write_op",
    "XtQuantV1", "BigQmtV1", "PtradeV1", "get_dialect", "register_dialect",
]

#: 注册表：新增客户端只需在这里登记（V4 §3.1「新增一个 dialect，不动编排层」）。
_DIALECTS: dict[str, type] = {
    XtQuantV1.dialect_id: XtQuantV1,
    BigQmtV1.dialect_id: BigQmtV1,
    PtradeV1.dialect_id: PtradeV1,
}


def register_dialect(cls: type) -> type:
    _DIALECTS[cls.dialect_id] = cls
    return cls


def get_dialect(dialect_id: str):
    try:
        return _DIALECTS[dialect_id]()
    except KeyError as exc:
        raise KeyError(
            f"未知方言: {dialect_id}（已注册: {', '.join(sorted(_DIALECTS))}）") from exc
