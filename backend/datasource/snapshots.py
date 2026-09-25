"""可复现 Dataset Snapshot 与跨源数据质量契约（Phase 5-7）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from core.clock import now_iso as _now  # 唯一实现在 core.clock（V11 R8 收敛）


def _digest(rows: Iterable[dict]) -> str:
    canonical = [dict(sorted(row.items())) for row in rows]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ReconcileResult:
    state: str
    primary_count: int
    secondary_count: int
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "primary_count": self.primary_count,
            "secondary_count": self.secondary_count,
            "missing": list(self.missing),
            "mismatched": list(self.mismatched),
        }


def reconcile_bars(primary: Iterable[dict], secondary: Iterable[dict],
                   tolerance: float = 0.0) -> ReconcileResult:
    """按交易日对账真实数据；缺失和价格差异都显式进入质量状态。"""
    left = {str(row.get("time")): row for row in primary}
    right = {str(row.get("time")): row for row in secondary}
    missing = sorted(set(left) - set(right))
    mismatched = []
    for dt in sorted(set(left) & set(right)):
        a, b = left[dt].get("close"), right[dt].get("close")
        if a is None or b is None:
            if a != b:
                mismatched.append(dt)
            continue
        if abs(float(a) - float(b)) > tolerance:
            mismatched.append(dt)
    state = "match" if not missing and not mismatched else "mismatch"
    return ReconcileResult(state, len(left), len(right), tuple(missing), tuple(mismatched))


def quality_issues(rows: Iterable[dict]) -> list[str]:
    """数据质量 Gate（V9 §11）：对将要发布的数据批次做规则校验，返回问题清单。

    规则：OHLC 合法性（high>=max(o,c)>=min(o,c)>=low）、volume/amount 非负、
    同一 (code,dt) 重复记录、dt 缺失。空清单表示通过。
    """
    issues: list[str] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        code = str(r.get("code", ""))
        dt = str(r.get("dt") or r.get("time") or "")
        where = f"{code or '?'}@{dt or '?'}"
        if not dt:
            issues.append(f"{where}: dt 缺失")
        key = (code, dt)
        if key in seen:
            issues.append(f"{where}: 同批次重复记录")
        seen.add(key)
        o, h, l, c = (r.get("open"), r.get("high"), r.get("low"), r.get("close"))
        nums = [o, h, l, c]
        if all(isinstance(v, (int, float)) and v is not None for v in nums):
            o_, h_, l_, c_ = float(o), float(h), float(l), float(c)
            if h_ < l_:
                issues.append(f"{where}: high<low")
            if o_ > 0 and c_ > 0 and (h_ < max(o_, c_) or l_ > min(o_, c_)):
                issues.append(f"{where}: OHLC 越界")
            if min(o_, h_, l_, c_) < 0:
                issues.append(f"{where}: 价格为负")
        for k in ("volume", "amount"):
            v = r.get(k)
            if isinstance(v, (int, float)) and v is not None and float(v) < 0:
                issues.append(f"{where}: {k} 为负")
    return issues


class DatasetSnapshotStore:
    """把数据批次发布为带版本、范围、checksum、质量状态的不可变元数据。"""

    def __init__(self, db):
        self.db = db

    def publish(self, dataset_id: str, version: str, provider_id: str,
                batch_id: str, rows: list[dict], *, quality_state: str,
                manifest: dict | None = None,
                calendar_version: str = "", adjustment_version: str = "") -> dict:
        if not dataset_id or not version or not provider_id or not batch_id:
            raise ValueError("dataset snapshot 必须提供 dataset_id/version/provider_id/batch_id")
        # V9 §11：质量 Gate 接线——发布即校验。规则违例不允许伪装成 complete/match：
        # 降级为 provisional 并把问题清单写进 manifest，研究/回测消费方可显式拒绝。
        issues = quality_issues(rows)
        manifest = dict(manifest or {})
        if issues:
            manifest["quality_issues"] = issues[:200]
            manifest["quality_issue_count"] = len(issues)
            if quality_state in ("complete", "match"):
                quality_state = "provisional"
        # ★ 空批次**绝不允许**标成 complete/final。
        #   实测（2026-09-20）：批次号传错时回查为 0 行，快照却照旧写着
        #   ``quality_state=complete``、``row_count=0`` —— 一份自称「完整」、
        #   实际不含任何行的数据集元数据。下游研究/回测据此放行等于消费空气。
        if not rows:
            manifest["empty_batch"] = (
                f"批次 {batch_id or '(空)'} 在 local_bars 中回查为 0 行 —— "
                "批次号与落库批次不匹配，或本次同步未写入任何数据；"
                "该快照不代表任何真实数据集，请勿据此判断数据完整性。")
            quality_state = "empty"
        record = {
            "dataset_id": dataset_id,
            "version": version,
            "provider_id": provider_id,
            "batch_id": batch_id,
            "as_of": _now(),
            "coverage_start": coverage[0] if (coverage := [d for d in sorted(
                str(r.get("dt") or r.get("time") or "") for r in rows) if d]) else "",
            "coverage_end": coverage[-1] if coverage else "",
            "row_count": len(rows),
            "checksum": _digest(rows),
            "quality_state": quality_state,
            "calendar_version": str(calendar_version or ""),
            "adjustment_version": str(adjustment_version or ""),
            "manifest_json": json.dumps(manifest, ensure_ascii=False,
                                          sort_keys=True, default=str),
            "created_at": _now(),
        }
        record["id"] = hashlib.sha256(
            f"{dataset_id}:{version}".encode("utf-8")).hexdigest()
        self.db.upsert("dataset_snapshots", record)
        return self.get(record["id"]) or record

    def publish_local_bars(self, dataset_id: str, version: str, provider_id: str,
                           batch_id: str, *, quality_state: str,
                           manifest: dict | None = None,
                           calendar_version: str = "",
                           adjustment_version: str = "") -> dict:
        # ★ 只按 **batch_id** 回查，不再要求 provider_id 同时匹配。
        #
        # ``local_bars.provider_id`` 写的是**每一行的真实命中来源**（broker / tencent /
        # sina…，见 ``app/sync/bars.py::_resolve_provider_id``）—— 一次全市场同步里
        # 不同标的可能落到不同源，所以「一个 provider_id」根本不是这批数据的身份。
        # 批次号 ``bars-<hex>`` 才是。
        #
        # 此前用 ``(provider_id='auto', batch_id=<ISO 时间戳>)`` 查询，两个条件都不成立
        # （落库用的是真实源名 + 内部生成的批次号）⇒ **永远命中 0 行**，于是每个快照的
        # ``row_count`` 恒为 0、``coverage_start/end`` 恒为空，却还标着 ``complete``。
        # 参数里的 ``provider_id`` 现仅作为「本次**请求**的数据源」写入元数据。
        rows = self.db.query(
            "SELECT code,period,adjust,dt,open,high,low,close,volume,amount,"
            "provider_id,batch_id,checksum,schema_version,quality_state "
            "FROM local_bars WHERE batch_id=? ORDER BY code,dt",
            (batch_id,))
        return self.publish(dataset_id, version, provider_id, batch_id, rows,
                            quality_state=quality_state, manifest=manifest,
                            calendar_version=calendar_version,
                            adjustment_version=adjustment_version)

    def latest_batch_id(self) -> str:
        """最近一次写入 ``local_bars`` 的批次号（未显式给批次时的回退值）。

        判据用 ``MAX(rowid)`` 而不是 ``MAX(dt)``：``dt`` 是**行情日期**，全量回补
        时同一个批次里也会出现很老的日期，按它排序会挑错批次；``rowid`` 才是
        「这一行是什么时候写进来的」。
        """
        rows = self.db.query(
            "SELECT batch_id FROM local_bars GROUP BY batch_id "
            "ORDER BY MAX(rowid) DESC LIMIT 1")
        return str(rows[0]["batch_id"]) if rows else ""

    def get(self, snapshot_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM dataset_snapshots WHERE id=?",
                                 (snapshot_id,))

    def latest(self, dataset_id: str) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM dataset_snapshots WHERE dataset_id=? "
            "ORDER BY created_at DESC LIMIT 1", (dataset_id,))

    def list_recent(self, dataset_id: str, limit: int = 20) -> list[dict]:
        """按创建时间倒序列出某数据集的快照（界面列表用）。

        ★ 列**显式点名**而不是 ``SELECT *``：``manifest_json`` 之外将来可能新增
        列（如 ``calendar_version``），``SELECT *`` 会静默把它们塞进响应体 ——
        一个「列出快照」的接口没有理由把新列顺带发出去。

        ★ ``limit`` 的上限（200）由本层强制：调用方传 100000 不该真的去读
        100000 行。这是仓储的取数策略，不是请求参数校验。
        """
        return self.db.query(
            "SELECT id,dataset_id,version,provider_id,batch_id,as_of,coverage_start,"
            "coverage_end,row_count,checksum,quality_state,manifest_json,created_at "
            "FROM dataset_snapshots WHERE dataset_id=? ORDER BY created_at DESC LIMIT ?",
            (dataset_id, max(1, min(int(limit), 200))),
        )


def require_quality(snapshot: dict, allowed: tuple[str, ...] = ("complete", "match")) -> None:
    state = snapshot.get("quality_state")
    if state not in allowed:
        raise ValueError(f"dataset snapshot 质量门禁失败：{state or 'unknown'}")


__all__ = ["DatasetSnapshotStore", "ReconcileResult", "reconcile_bars",
           "require_quality", "quality_issues"]
