"""运行时配置中心（配置灵活化）：引擎级参数运行期热更新，无需重启。

设计：
- `runtime_config` 表存 key -> value(JSON 文本)；未配置的 key 使用 `_DEFAULTS`。
- 引擎（sync 微批/快照/补发、条件单轮询、健康检查、对账巡检）在每次循环迭代
  读取最新值，因此修改后**立即生效**（热更新）。
- REST：`GET /config/runtime` 读全部（含生效值/默认值/说明）；
  `PUT /config/runtime` 批量更新（校验类型，写审计）。

配置项命名：`域.参数`，例如 `sync.batch_window`。
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("qmt_work.runtime_config")


class RuntimeConfig:
    # key -> (默认值, 类型, 最小允许值, 说明)
    _DEFAULTS: dict[str, tuple] = {
        # ---- 行情同步引擎 ----
        "sync.batch_window": (0.1, float, 0.02,
                              "行情微批聚合窗口（秒）：越大帧数越少、延迟越高"),
        "sync.snapshot_interval": (5.0, float, 1.0,
                                   "账户快照归档间隔（秒）：净值/持仓/资金落库频率"),
        "sync.recent_window": (30.0, float, 5.0,
                               "WS 断线补发窗口（秒）：重连订阅后补发最近 N 秒行情"),
        # ---- 条件单引擎 ----
        "condition.interval": (2.0, float, 0.5,
                               "条件单/止损单轮询行情间隔（秒）"),
        # ---- 连接健康 ----
        "health.check_interval": (5.0, float, 1.0,
                                  "券商连接健康检查间隔（秒）"),
        # ---- 委托对账 ----
        "reconcile.interval": (300.0, float, 10.0,
                               "委托对账核销巡检间隔（秒）：WAL 未核销委托 vs 券商当日委托"),
        # ---- 算法单引擎 ----
        "algo.poll_interval": (1.0, float, 0.2,
                               "算法单拆单轮询间隔（秒）"),
        # ---- 涨停监控 ----
        "limitup.poll_interval": (0.5, float, 0.1,
                                  "涨停监控轮询 tick 间隔（秒）"),
    }

    def __init__(self, db=None):
        self.db = db
        self._cache: dict | None = None

    # ---------------- 读取 ----------------
    def invalidate(self) -> None:
        self._cache = None

    def _load(self) -> dict:
        if self._cache is None:
            self._cache = {}
            if self.db is not None:
                try:
                    rows = self.db.query("SELECT key, value FROM runtime_config")
                    for r in rows:
                        k = r.get("key", "")
                        v = r.get("value")
                        try:
                            self._cache[k] = json.loads(v) if v not in (None, "") else None
                        except (json.JSONDecodeError, TypeError):
                            self._cache[k] = v
                except Exception as exc:  # noqa: BLE001
                    log.warning("runtime config load failed: %s", exc)
        return self._cache

    def get(self, key: str):
        """读取生效值：DB 覆盖 > 默认值。"""
        if key not in self._DEFAULTS:
            return None
        default, _, _, _ = self._DEFAULTS[key]
        store = self._load()
        if key in store and store[key] is not None:
            try:
                return type(default)(store[key])
            except (TypeError, ValueError):
                return default
        return default

    def get_float(self, key: str) -> float:
        v = self.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(self._DEFAULTS[key][0])

    def all(self) -> dict:
        """返回全部配置：{key: {value, default, type, min, desc}}。"""
        store = self._load()
        out = {}
        for key, (default, vtype, vmin, desc) in self._DEFAULTS.items():
            val = store.get(key)
            if val is None:
                val = default
            else:
                try:
                    val = vtype(val)
                except (TypeError, ValueError):
                    val = default
            out[key] = {
                "value": val, "default": default, "type": vtype.__name__,
                "min": vmin, "desc": desc,
                "overridden": key in store and store[key] is not None,
            }
        return out

    # ---------------- 更新 ----------------
    def set_many(self, data: dict) -> list[str]:
        """批量更新；返回实际变更的 key 列表（含类型/下限校验）。"""
        changed = []
        store = self._load()
        for key, raw in data.items():
            if key not in self._DEFAULTS:
                raise ValueError(f"未知配置项：{key}")
            default, vtype, vmin, _ = self._DEFAULTS[key]
            try:
                val = vtype(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 须为 {vtype.__name__} 类型") from exc
            if val < vmin:
                raise ValueError(f"{key} 不能小于 {vmin}")
            if store.get(key) != val:
                old_val = store.get(key)
                store[key] = val
                if self.db is not None:
                    self.db.upsert("runtime_config", {
                        "key": key,
                        "value": json.dumps(val, ensure_ascii=False),
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    self._record_history(key, "set", old_val, val)
                changed.append(key)
        if changed:
            self.invalidate()
        return changed

    def reset(self, key: str = "") -> list[str]:
        """恢复默认：重置全部或指定 key。"""
        changed = []
        store = self._load()
        keys = [key] if key else list(self._DEFAULTS)
        for k in keys:
            if k in store and store[k] is not None:
                if self.db is not None:
                    self.db.execute("DELETE FROM runtime_config WHERE key=?", (k,))
                    self._record_history(k, "reset", store[k], None)
                store.pop(k, None)
                changed.append(k)
        if changed:
            self.invalidate()
        return changed

    # ---------------- 变更历史 / 回滚 ----------------
    def _record_history(self, key: str, action: str, old, new) -> None:
        """写入 config_history，形成可追溯的变更链。"""
        if self.db is None:
            return
        try:
            self.db.insert("config_history", {
                "key": key, "action": action,
                "old_value": json.dumps(old, ensure_ascii=False) if old is not None else "",
                "new_value": json.dumps(new, ensure_ascii=False) if new is not None else "",
                "actor": "system", "ip": "",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("config history record failed: %s", exc)

    def history(self, limit: int = 50) -> list[dict]:
        """返回最近 limit 条变更历史（倒序）。"""
        if self.db is None:
            return []
        return self.db.query(
            "SELECT id, key, action, old_value, new_value, actor, created_at "
            "FROM config_history ORDER BY id DESC LIMIT ?", (limit,))

    def rollback(self, entry_id: int) -> bool:
        """回滚到指定历史记录：将该记录的 old_value 重新写回（会再记一条 history）。"""
        if self.db is None:
            return False
        row = self.db.query_one(
            "SELECT id, key, old_value FROM config_history WHERE id=?", (entry_id,))
        if not row:
            return False
        key = row["key"]
        if key not in self._DEFAULTS:
            return False
        raw = row["old_value"]
        if not raw:
            self.reset(key)        # 回滚到默认（删除覆盖）
        else:
            try:
                val = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return False
            self.set_many({key: val})
        self.invalidate()
        return True

    # ---------------- 便捷属性（引擎热更新用） ----------------
    @property
    def batch_window(self) -> float:
        return self.get_float("sync.batch_window")

    @property
    def snapshot_interval(self) -> float:
        return self.get_float("sync.snapshot_interval")

    @property
    def recent_window(self) -> float:
        return self.get_float("sync.recent_window")

    @property
    def condition_interval(self) -> float:
        return self.get_float("condition.interval")

    @property
    def health_check_interval(self) -> float:
        return self.get_float("health.check_interval")

    @property
    def reconcile_interval(self) -> float:
        return self.get_float("reconcile.interval")
