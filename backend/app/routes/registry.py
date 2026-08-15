"""券商注册表 V2 路由：能力协商、档案列表、热插拔。

- GET  /brokers/registry              列出全部券商档案 + 推导能力
- GET  /brokers/registry/{id}         单券商档案详情 + 全部能力
- POST /brokers/registry/negotiate    能力协商（给定请求能力，返回支持/不支持）
- POST /brokers/registry/profiles     热插拔：运行期新增/覆盖券商档案
- POST /brokers/registry/reload       重新加载基线档案
"""
from app.routes._common import ok, err, state
from fastapi import APIRouter

router = APIRouter()


@router.get("/brokers/registry")
async def registry_list():
    from xtquant_client.registry import list_profiles_v2
    return ok({"count": len(list_profiles_v2()), "profiles": list_profiles_v2()})


@router.get("/brokers/registry/{broker_id}")
async def registry_detail(broker_id: str):
    from xtquant_client.registry import registry as reg
    p = reg.get(broker_id)
    if p is None:
        return err(404, f"未知券商：{broker_id}")
    return ok({
        "id": p.id, "name": p.name, "adapter": p.adapter,
        "supported_account_types": p.supported_account_types,
        "supported_periods": p.supported_periods,
        "sdk_required": p.sdk_required, "min_version": p.min_version,
        "capabilities": reg.effective_capabilities(p),
        "features": p.features, "note": p.note,
    })


@router.post("/brokers/registry/negotiate")
async def registry_negotiate(body: dict):
    broker_id = body.get("broker_id", "")
    requested = body.get("capabilities") or []
    from xtquant_client.registry import negotiate_capabilities
    try:
        return ok(negotiate_capabilities(broker_id, requested))
    except Exception as exc:  # noqa: BLE001
        return err(400, str(exc))


@router.post("/brokers/registry/profiles")
async def registry_hotplug(body: dict):
    from xtquant_client.registry import hotplug_profile, registry as reg
    try:
        p = hotplug_profile(body)
    except ValueError as exc:
        return err(400, str(exc))
    return ok({"id": p.id, "name": p.name, "adapter": p.adapter,
               "capabilities": reg.effective_capabilities(p)})


@router.post("/brokers/registry/reload")
async def registry_reload():
    from xtquant_client.registry import registry as reg
    n = reg.reload()
    return ok({"reloaded": True, "count": n})
