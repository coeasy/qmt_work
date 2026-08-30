"""G10-4 统一导出：CSV / Excel / JSON（列定义复用标准模型字段名）。

用法：
    columns = [{"key": "code", "label": "代码"}, {"key": "close", "label": "收盘"}]
    to_csv(rows, columns)              # -> str（可直接返回给前端下载）
    to_excel(rows, columns, path)      # -> 写 .xlsx（pandas 可用时；否则回退 CSV）
    to_json(rows, path=None)           # -> str 或写 .json

铁律：缺失列值输出空串，不估算；字段顺序与列定义一致（可复现）。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional

_COL_TYPE = List[Dict[str, Any]]       # columns: [{key, label}]


def _normalize_columns(columns: _COL_TYPE) -> _COL_TYPE:
    return [{"key": str(c.get("key")), "label": str(c.get("label") or c.get("key"))}
            for c in columns]


def to_csv(rows: List[dict], columns: _COL_TYPE) -> str:
    cols = _normalize_columns(columns)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c["label"] for c in cols])
    for r in rows:
        writer.writerow([r.get(c["key"], "") for c in cols])
    return buf.getvalue()


def to_json(rows: List[dict], path: Optional[str] = None) -> str:
    text = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    if path:
        _write(path, text)
    return text


def to_excel(rows: List[dict], columns: _COL_TYPE, path: str) -> str:
    """写 .xlsx（pandas 可用）；不可用时回退 CSV 并返回其内容。"""
    try:
        import pandas as pd  # noqa: WPS433
    except ImportError:
        return to_csv(rows, columns)
    cols = _normalize_columns(columns)
    df = pd.DataFrame(
        [{c["key"]: r.get(c["key"], "") for c in cols} for r in rows],
        columns=[c["key"] for c in cols],
    )
    df.to_excel(path, index=False, engine="openpyxl") if path.endswith(".xlsx") \
        else df.to_csv(path, index=False, encoding="utf-8-sig")
    return to_csv(rows, columns)


def _write(path: str, text: str) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


__all__ = ["to_csv", "to_json", "to_excel"]
