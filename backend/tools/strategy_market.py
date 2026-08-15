"""策略市场：内置模板目录 + 用户策略发布/安装/导入导出（JSON + .zip）。

所有持久化走 state.db（SQLite，应用自管建表）。无需券商连接，纯函数可单测。
"""
import json
import uuid
import zipfile
from datetime import datetime, timezone

from app.state import state
from tools.strategy_gen import _TEMPLATES, save_qmt_strategy

# 策略市场表名（state.db 自管建表）
_TABLE = "strategy_market"

# 各内置模板的参数 schema（hardcode，供 catalog 展示与前端渲染）
_PARAM_SCHEMAS = {
    "ma_cross": [
        {"name": "fast", "default": 5, "type": "int"},
        {"name": "slow", "default": 20, "type": "int"},
        {"name": "volume", "default": 100, "type": "int"},
    ],
    "macd": [
        {"name": "fast", "default": 12, "type": "int"},
        {"name": "slow", "default": 26, "type": "int"},
        {"name": "signal", "default": 9, "type": "int"},
        {"name": "volume", "default": 100, "type": "int"},
    ],
    "rsi": [
        {"name": "period", "default": 14, "type": "int"},
        {"name": "buy_at", "default": 30, "type": "float"},
        {"name": "sell_at", "default": 70, "type": "float"},
        {"name": "volume", "default": 100, "type": "int"},
    ],
    "limitup": [
        {"name": "codes", "default": "600519.SH", "type": "str"},
        {"name": "limit_pct", "default": 0.1, "type": "float"},
        {"name": "cutoff", "default": "10:00", "type": "str"},
        {"name": "buy_volume", "default": 100, "type": "int"},
    ],
}

_TYPE_NAMES = {
    "ma_cross": "双均线金叉",
    "macd": "MACD 金叉",
    "rsi": "RSI 超买超卖",
    "limitup": "涨停打板",
}

_TYPE_DESCS = {
    "ma_cross": "快/慢均线金叉买入、死叉卖出。",
    "macd": "MACD 快慢线 DIF/DEA 金叉买入、死叉卖出。",
    "rsi": "RSI 低于买点买入、高于卖点卖出。",
    "limitup": "监控涨停时间窗 + tick 涨幅打板。",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_name(s: str) -> str:
    s = (s or "strategy").replace("/", "_")
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in s)


def _ensure_table(db) -> None:
    """惰性建表（幂等）。依赖 state.db 自管连接。"""
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        "id TEXT PRIMARY KEY, title TEXT DEFAULT '', author TEXT DEFAULT '', "
        "description TEXT DEFAULT '', type TEXT DEFAULT '', content TEXT DEFAULT '', "
        "tags_json TEXT DEFAULT '[]', created_at TEXT, downloads INTEGER DEFAULT 0)")


def _row_to_dict(row) -> dict | None:
    if not row:
        return None
    out = dict(row)
    try:
        out["tags"] = json.loads(out.get("tags_json") or "[]")
    except (ValueError, TypeError):
        out["tags"] = []
    return out


# ---------------- 内置模板目录 ----------------
def strategy_catalog() -> list[dict]:
    """返回内置模板描述（来自 _TEMPLATES 的 4 类）。"""
    out = []
    for tid in _TEMPLATES:
        out.append({
            "id": tid,
            "name": _TYPE_NAMES.get(tid, tid),
            "type": tid,
            "description": _TYPE_DESCS.get(tid, ""),
            "params_schema": _PARAM_SCHEMAS.get(tid, []),
        })
    return out


# ---------------- 发布 / 列表 / 取单 ----------------
def publish_to_market(strategy_id: str, meta: dict, content: str) -> dict:
    """把一条用户策略持久化到 strategy_market 表。"""
    if state.db is None:
        raise RuntimeError("数据库未初始化")
    db = state.db
    _ensure_table(db)
    meta = dict(meta or {})
    sid = strategy_id or meta.get("id") or f"usr_{uuid.uuid4().hex[:12]}"
    rec = {
        "id": sid,
        "title": meta.get("title", sid),
        "author": meta.get("author", "anonymous"),
        "description": meta.get("description", ""),
        "type": meta.get("type", "custom"),
        "content": content,
        "tags_json": json.dumps(meta.get("tags", []) or [], ensure_ascii=False),
        "created_at": _now(),
        "downloads": 0,
    }
    db.upsert(_TABLE, rec)
    return _row_to_dict(db.query_one(f"SELECT * FROM {_TABLE} WHERE id=?", (sid,)))


def list_market(limit: int = 50, tag: str | None = None) -> list[dict]:
    if state.db is None:
        return []
    db = state.db
    _ensure_table(db)
    rows = db.query(f"SELECT * FROM {_TABLE} ORDER BY created_at DESC LIMIT ?", (int(limit),))
    if tag:
        rows = [r for r in rows if tag in json.loads(r.get("tags_json") or "[]")]
    return [_row_to_dict(r) for r in rows]


def get_market(id: str) -> dict | None:
    if state.db is None:
        return None
    db = state.db
    _ensure_table(db)
    return _row_to_dict(db.query_one(f"SELECT * FROM {_TABLE} WHERE id=?", (id,)))


def install_from_market(id: str, client_path: str) -> dict:
    """从市场取一条策略，写入 QMT 客户端 mpython 目录，返回写入结果。"""
    row = get_market(id)
    if not row:
        raise ValueError(f"市场策略不存在：{id}")
    filename = _safe_name(row.get("id") or "strategy")
    res = save_qmt_strategy(filename, row.get("content", ""), client_path or "")
    db = state.db
    if db is not None:
        db.execute(f"UPDATE {_TABLE} SET downloads = downloads + 1 WHERE id=?", (id,))
    return res


# ---------------- 批量 .zip 导出/导入 ----------------
def export_bundle(strategy_ids: list[str], out_path: str) -> dict:
    if state.db is None:
        raise RuntimeError("数据库未初始化")
    db = state.db
    _ensure_table(db)
    manifest = {"version": 1, "exported_at": _now(), "strategies": []}
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for sid in strategy_ids:
            row = db.query_one(f"SELECT * FROM {_TABLE} WHERE id=?", (sid,))
            if not row:
                continue
            fname = _safe_name(row.get("id") or f"strategy_{count}") + ".py"
            z.writestr(fname, row.get("content", ""))
            manifest["strategies"].append({
                "id": row.get("id"),
                "title": row.get("title"),
                "author": row.get("author"),
                "description": row.get("description"),
                "type": row.get("type"),
                "filename": fname,
                "tags": json.loads(row.get("tags_json") or "[]"),
                "created_at": row.get("created_at"),
            })
            count += 1
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"path": out_path, "count": count}


def import_bundle(zip_path: str) -> dict:
    if state.db is None:
        raise RuntimeError("数据库未初始化")
    db = state.db
    _ensure_table(db)
    imported = 0
    ids: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            raise ValueError("无效的 bundle：缺少 manifest.json")
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        for item in manifest.get("strategies", []):
            fname = item.get("filename")
            if not fname or fname not in names:
                continue
            content = z.read(fname).decode("utf-8")
            rec = {
                "id": item.get("id"),
                "title": item.get("title", item.get("id", "")),
                "author": item.get("author", "anonymous"),
                "description": item.get("description", ""),
                "type": item.get("type", "custom"),
                "content": content,
                "tags_json": json.dumps(item.get("tags", []) or [], ensure_ascii=False),
                "created_at": item.get("created_at") or _now(),
                "downloads": 0,
            }
            db.upsert(_TABLE, rec)
            imported += 1
            ids.append(rec["id"])
    return {"imported": imported, "ids": ids}


# ---------------- 单策略 JSON 导出/导入 ----------------
def export_json(strategy_id: str, out_path: str) -> dict:
    row = get_market(strategy_id)
    if not row:
        raise ValueError(f"策略不存在：{strategy_id}")
    payload = {
        "id": row.get("id"),
        "title": row.get("title"),
        "author": row.get("author"),
        "description": row.get("description"),
        "type": row.get("type"),
        "content": row.get("content"),
        "tags": json.loads(row.get("tags_json") or "[]"),
        "created_at": row.get("created_at"),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"path": out_path, "id": strategy_id}


def import_json(json_path: str) -> dict:
    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if state.db is None:
        raise RuntimeError("数据库未初始化")
    db = state.db
    _ensure_table(db)
    rec = {
        "id": payload.get("id"),
        "title": payload.get("title", payload.get("id", "")),
        "author": payload.get("author", "anonymous"),
        "description": payload.get("description", ""),
        "type": payload.get("type", "custom"),
        "content": payload.get("content", ""),
        "tags_json": json.dumps(payload.get("tags", []) or [], ensure_ascii=False),
        "created_at": payload.get("created_at") or _now(),
        "downloads": 0,
    }
    db.upsert(_TABLE, rec)
    return {"imported": 1, "ids": [rec["id"]]}
