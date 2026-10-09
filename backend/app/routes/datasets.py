"""R28 数据集 API：可下载数据清单 / 同步 / 本地查询。

对标 free-stockdb 的「数据 → 本地查询 → 多种调用方式」闭环，但**不复制它的存储
选型**（Zstd + C++ 时序引擎）：qmt_work 已有 SQLite 数据仓、能力链与 JobRuntime，
这里做的是把既有能力**组织成数据集**并暴露出来，而不是另起一套引擎。

端点（全部 ``/api/v1/datasets/*``）：

- ``GET  /datasets``            数据集清单 + 每项运行时状态（本地行数 / 最后同步）
- ``GET  /datasets/sources``    当前源可用性（券商 vs 第三方），回答「现在会从哪下」
- ``GET  /datasets/{id}``       单个数据集详情
- ``POST /datasets/{id}/sync``  触发一次同步（默认限 50 只，防止请求挂死）
- ``GET  /datasets/{id}/data``  查询**本地已下载**的数据（离线可用）

## 契约

- 业务失败走 ``code != 0`` 的 envelope，HTTP 恒 200（前端可统一取 ``message``）；
- **未连券商不粉饰**：财务等仅券商数据集在无券商时明确返回失败与原因，
  绝不返回空列表冒充成功；
- 状态里的行数是**真实 COUNT**，不是估算；查不到就是 0，不拿配置值填充。
"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.routes._common import err, ok

router = APIRouter()

#: 手动触发同步时的默认标的上限。全市场同步是**调度**的事（分钟级到小时级），
#: HTTP 请求必须能在超时前返回——否则用户只能看到一个转圈的按钮。
DEFAULT_SYNC_LIMIT = 50
MAX_SYNC_LIMIT = 500


class SyncBody(BaseModel):
    mode: str = "incremental"
    limit: int = DEFAULT_SYNC_LIMIT
    concurrency: int = 4
    source: str = "auto"
    dry_run: bool = False
    codes: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# 状态统计
# ---------------------------------------------------------------------------
def _count_table(store: str, period: str = "") -> dict:
    """统计某张数据集表的真实行数 / 覆盖区间。

    SQL 全部收在 ``app/services/dataset_store.py``（路由层不出现裸 SQL，见
    ``scripts/check_execution_architecture.py``）。这里只负责把分钟线独立库的
    统计并进同一形状。
    """
    from app.services.dataset_store import count_table

    if store == "intraday":
        try:
            from datasource.intraday_store import get_intraday_store
            st = get_intraday_store().stats()
            p = st.get("periods", {}).get(period or "", {})
            if not p:
                return {"rows": 0, "codes": 0, "first_dt": "", "last_dt": ""}
            return {"rows": int(p.get("rows") or 0), "codes": int(p.get("codes") or 0),
                    "first_dt": p.get("first_dt") or "", "last_dt": p.get("last_dt") or ""}
        except Exception:  # noqa: BLE001 分钟库未建立时按空处理
            return {"rows": 0, "codes": 0, "first_dt": "", "last_dt": ""}
    return count_table(store, period)


def _last_sync(dataset_id: str) -> str:
    from datasource.local_store import get_store

    try:
        return str(get_store().get_meta(f"dataset.{dataset_id}.last_sync_at", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _status(spec) -> dict:
    return {
        **spec.as_dict(),
        "local": _count_table(spec.store, spec.period),
        "last_sync_at": _last_sync(spec.id),
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/datasets")
async def dataset_list():
    """数据集清单 + 运行时状态。

    前端据此渲染「数据中心」：能下载什么、现在库里有多少、上次什么时候同步的。
    """
    from datasource import datasets as DS

    grouped: dict[str, list] = {}
    for spec in DS.DATASETS.values():
        grouped.setdefault(spec.category, []).append(_status(spec))
    return ok({
        "total": len(DS.DATASETS),
        "categories": [
            {"id": c, "label": DS.CATEGORY_LABEL.get(c, c), "items": grouped[c]}
            for c in sorted(grouped)
        ],
    })


@router.get("/datasets/sources")
async def dataset_sources():
    """当前数据源可用性 —— 回答「现在点同步，数据会从哪来」。

    ★ 这就是「QMT 优先、否则第三方」的**运行时真相**：链的**声明顺序**在
    ``DataSetSpec.chain`` 里，但真正被选中谁由 ``ProviderCatalog.resolve_chain``
    按 能力校验 → 依赖可用 → 商用许可 → 熔断状态 决定。本端点把两者都返回，
    让用户能看出「声明想用券商，但现在券商没连，所以实际走 tdx」。
    """
    from datasource import datasets as DS
    from datasource.providers import provider_catalog
    from datasource.registry import get_hub

    health = {}
    try:
        health = await get_hub().health()
    except Exception as exc:  # noqa: BLE001 健康检查失败不该让端点 500
        health = {"_error": str(exc)}

    broker_available = bool((health.get("broker") or {}).get("available"))
    providers = provider_catalog.describe()

    resolved: dict[str, dict] = {}
    for spec in DS.DATASETS.values():
        try:
            # ★ 走 ``chain_for`` 而不是 ``ProviderCatalog.resolve_chain``：前者带
            #   注册态与熔断，是「此刻真正会试的顺序」；后者只是声明过滤。
            chain = get_hub().chain_for(spec.capability)
        except Exception:  # noqa: BLE001
            chain = []
        # ★ 内置源（``local``）不是注册 provider，会被 resolve_chain 过滤掉 ⇒
        #   ``calendar`` 的解析链恒为空。若直接让前端显示「无可用源」就是假告警
        #   （日历其实每次都能同步成功）。把「解析为空但有内置实现」单独报出来。
        builtin = ""
        if not chain:
            builtin = next((s for s in spec.chain if s in DS.BUILTIN_SOURCES), "")
        resolved[spec.id] = {
            "declared": list(spec.chain),
            "resolved": list(chain),
            "effective_first": (chain[0] if chain else ""),
            "builtin_fallback": builtin,
            "builtin_note": DS.BUILTIN_SOURCES.get(builtin, ""),
        }
    return ok({
        "broker_available": broker_available,
        "broker_note": (health.get("broker") or {}).get("note", ""),
        "providers": providers,
        "health": health,
        "per_dataset": resolved,
    })


@router.get("/datasets/{dataset_id}")
async def dataset_detail(dataset_id: str):
    from datasource import datasets as DS

    spec = DS.get(dataset_id)
    if spec is None:
        return err(400, f"未知数据集：{dataset_id}")
    return ok(_status(spec))


@router.post("/datasets/{dataset_id}/sync")
async def dataset_sync(dataset_id: str, body: SyncBody | None = None):
    """触发一次同步。

    ⚠️ **默认只跑 50 只**：这是 HTTP 请求，不是调度任务。全市场同步请走
    ``POST /api/v1/runtime/schedules`` 建定时调度，或用 ``codes`` 明确指定标的。

    ``dry_run=true`` 只取数不落库，用于「这个源通不通」的快速验证。
    """
    from datasource import datasets as DS

    spec = DS.get(dataset_id)
    if spec is None:
        return err(400, f"未知数据集：{dataset_id}")
    body = body or SyncBody()
    limit = int(body.limit or 0)
    if limit < 0:
        limit = 0
    if limit > MAX_SYNC_LIMIT and not body.codes:
        return err(400, f"limit 超过单次上限 {MAX_SYNC_LIMIT}；"
                        "全市场同步请改用定时调度")

    from app.sync.datasets import DatasetSyncer

    try:
        syncer = DatasetSyncer(
            dataset_id, mode=body.mode, limit=limit,
            concurrency=max(1, min(8, int(body.concurrency or 4))),
            source=body.source, dry_run=body.dry_run, codes=body.codes)
        summary = await syncer.sync()
    except KeyError:
        return err(400, f"未知数据集：{dataset_id}")
    except Exception as exc:  # noqa: BLE001 同步异常转成业务失败，不让前端看 500
        return err(503, f"同步失败：{exc}")

    result = summary.as_dict()
    result["local"] = _count_table(spec.store, spec.period)
    # 同步成功但一行没写 = 数据源不可用。明说，不让界面显示「已完成」。
    if not summary.ok:
        return err(503, "；".join(summary.problems) or "同步未完成", result)
    return ok(result)


@router.get("/datasets/{dataset_id}/data")
async def dataset_data(dataset_id: str, code: str = "", limit: int = 200,
                       start: str = "", end: str = ""):
    """查询**本地已下载**的数据（离线可用，不打远程源）。

    ★ 这是「下载来的数据能用」的证明：不提供这个端点，用户同步完只能靠信任。
    参考数据类（列表 / 板块 / 股本）忽略 ``code``，返回整批。
    """
    from datasource import datasets as DS

    spec = DS.get(dataset_id)
    if spec is None:
        return err(400, f"未知数据集：{dataset_id}")
    limit = max(1, min(int(limit or 200), 5000))

    try:
        rows = _read_local(spec, code, limit, start, end)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"读取本地数据失败：{exc}")
    return ok({"dataset": dataset_id, "code": code, "count": len(rows),
               "rows": rows})


def _read_local(spec, code: str, limit: int, start: str, end: str) -> list[dict]:
    """读本地数据。全部 SQL 在 ``app/services/dataset_store.py``。"""
    from app.services.dataset_store import read_local

    return read_local(spec, code, limit, start, end)
