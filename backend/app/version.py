"""后端版本号唯一来源（P2-28）。

版本单一真源：仓库根目录 ``VERSION`` 文件（Phase 9 ⑦）。
本模块读取并缓存；``VERSION`` 缺失/非法时回退内置值，绝不抛错阻断启动。
所有对外暴露版本（/health、/ready、FastAPI OpenAPI、release-manifest）
统一引用 ``__version__``。
"""
from __future__ import annotations

from pathlib import Path

_FALLBACK = "0.1.0"


def _load() -> str:
    for base in (Path(__file__).resolve().parents[2],   # 仓库根（源码运行）
                 Path(__file__).resolve().parents[1]):  # backend（打包形态）
        try:
            text = (base / "VERSION").read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return _FALLBACK


__version__ = _load()
