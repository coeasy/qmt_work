from core.context import AppContext, get_ctx
# --- stdlib imports injected by fix_route_imports ---
import hashlib
import json

from fastapi import APIRouter, Depends

from app.routes._common import Request, err, ok, settings

router = APIRouter()

@router.get("/signal/mode")
async def get_signal_mode(ctx: AppContext = Depends(get_ctx)):
    """获取signal / mode（GET /signal/mode）。"""
    if ctx.signal_router is None:
        return err(503, "信号路由未初始化")
    return ok({"mode": ctx.signal_router.mode})

@router.post("/signal/mode")
async def set_signal_mode(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交signal / mode（POST /signal/mode）。"""
    if ctx.signal_router is None:
        return err(503, "信号路由未初始化")
    try:
        mode = ctx.signal_router.set_mode(body.get("mode", "live"))
    except ValueError as exc:
        return err(400, str(exc))
    ctx.db.audit("admin", "signal.mode", "global", {"mode": mode}, "ok")
    return ok({"mode": mode})

@router.post("/signal/submit")
async def signal_submit(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交signal / submit（POST /signal/submit）。"""
    if ctx.signal_router is None:
        return err(503, "信号路由未初始化")
    from gateway.signal_router import Signal
    sig = Signal(
        source=body.get("source", "manual"),
        code=body.get("code", ""),
        side=body.get("side", "buy"),
        volume=int(body.get("volume", 0)),
        price=float(body.get("price", 0) or 0),
        price_type=body.get("price_type", "limit"),
        remark=body.get("remark", ""),
        broker_id=body.get("broker_id", ""),
        payload=body.get("payload", {}))
    # 阶段 4：idempotency_key 非空时经 submit() 单飞幂等（同 key 只执行一次真实逻辑）
    idem = str(body.get("idempotency_key", "") or "").strip()
    res = await ctx.signal_router.submit(
        code=sig.code, side=sig.side, volume=sig.volume, price=sig.price,
        price_type=sig.price_type, source=sig.source, broker_id=sig.broker_id,
        remark=sig.remark, idempotency_key=idem, payload=sig.payload)
    if isinstance(res, dict) and res.get("ok"):
        return ok(res)
    # ★★ 失败原因必须按 `broker_unavailable` 分流，**不能一律 503**（2026-09-23 R25）。
    #
    # `gateway/signal_router.py` 在两处**刻意**设置了 `broker_unavailable` 标志
    # （`_live()` 无券商会话 / 异常里 isinstance BrokerNotConnectedError|BrokerSDKError），
    # 注释也写明「路由据此返回 503 + 券商连接引导（而非 400）」—— 但路由层**从未读过它**，
    # 于是这个标志成了**孤儿**，并被「一律 503」吞掉。后果：
    #   ① 风控熔断 / 超出单笔限额 / 可用资金不足 / 参数非法，全都被报成
    #      「服务不可用」，用户被引导去查后端，而真正原因（风控拒绝）永远不出现；
    #   ② 该标志一旦被其它调用方（MCP / 前端）依赖，行为与注释不一致 ⇒ 说谎的契约。
    #
    # 语义边界（唯一真源就在这一处）：
    #   - `broker_unavailable=True`  ⇒ **503**：券商客户端没连上 / SDK 缺失 / 桥接不可用，
    #     这是「服务能力缺失」，给「去连接券商」的引导是对的；
    #   - 其余（风控拒绝 / 柜台拒单 / 参数错）⇒ **400**：请求本身被业务规则拒绝。
    # 真正的 503 还有一处：上面 `ctx.signal_router is None`（信号路由没起来）。
    if isinstance(res, dict) and res.get("broker_unavailable"):
        return err(503, res.get("reason", "未连接券商客户端"), res)
    return err(400, res.get("reason", "信号路由失败") if isinstance(res, dict) else "信号路由失败")

@router.post("/signal/confirm")
async def signal_confirm(body: dict, ctx: AppContext = Depends(get_ctx)):
    """二次确认：携带 confirm_token（及 TOTP 码）执行挂起的大额下单。"""
    if ctx.signal_router is None:
        return err(503, "信号路由未初始化")
    token = body.get("confirm_token", "")
    if not token:
        return err(400, "缺少 confirm_token")
    # ★★ TOTP 字段名必须两种都收（2026-09-20 实测修复）。
    #
    # 此前只读 `totp_code`，而前端 `signalApi.confirm()`（trade.ts）发的是 **`totp`**
    # —— 两侧各用一个名字，中间没有任何归一化。后果不是「少个参数」而是：
    # **一旦启用 TOTP，大额单的二次确认永远无法通过**，且报错文案把责任推给用户
    # （「TOTP 校验失败，请重新发起信号」），用户重试到天荒地老也不会成功。
    # 等于「开了安全开关就再也下不了大额单」。
    #
    # 实测对照（同一合法 TOTP 码、同一挂起令牌、mode=paper、本机真实库）：
    #   {"confirm_token":T,"totp":"231417"}      → 400「TOTP 校验失败，请重新发起信号」
    #   {"confirm_token":T,"totp_code":"231417"} → TOTP 通过，继续走到下一道真实校验
    # 唯一差异就是字段名。
    #
    # 归一化放在**后端**：前端（含已分发的 Electron 客户端）无需升级即可立即生效，
    # 同时接受新老两种写法。前端已同步改为发规范名 `totp_code`。
    totp_code = str(body.get("totp_code") or body.get("totp") or "")
    res = await ctx.signal_router.confirm(token, totp_code)
    if res.get("ok"):
        return ok(res)
    return err(400, res.get("reason", "确认失败"), res)

@router.post("/signal/webhook")
async def signal_webhook(request: Request, ctx: AppContext = Depends(get_ctx)):
    """外部策略系统信号入站：HMAC-SHA256 签名校验（QMT_WEBHOOK_SECRET 非空时），经统一信号路由。"""
    if ctx.signal_router is None:
        return err(503, "信号路由未初始化")
    import hmac
    secret = settings.webhook_secret
    raw = await request.body()
    if secret:
        provided = request.headers.get("x-signature", "")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            return err(401, "签名校验失败")
    elif not getattr(settings, "webhook_allow_insecure", False):
        # P0-7：未配置密钥 = 任何人都能构造请求实盘下单。默认拒绝，
        # 必须由运维显式开启 webhook_allow_insecure 才回到旧的不安全行为。
        return err(401, "未配置 webhook_secret，已拒绝未签名请求"
                        "（联调环境如需放行请显式设置 webhook_allow_insecure=true）")
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        return err(400, "invalid json body")
    from gateway.signal_router import Signal
    sig = Signal(
        source=body.get("source", "webhook"),
        code=body.get("code", ""), side=body.get("side", "buy"),
        volume=int(body.get("volume", 0)), price=float(body.get("price", 0) or 0),
        price_type=body.get("price_type", "limit"), remark=body.get("remark", ""),
        broker_id=body.get("broker_id", ""), payload=body.get("payload", {}))
    # P0-3：外部系统可显式传 idempotency_key 获得幂等（未传则不去重，保持旧行为）。
    res = await ctx.signal_router.route(
        sig, idempotency_key=str(body.get("idempotency_key", "") or "").strip())
    ctx.db.audit("webhook", "signal.submit", body.get("code", ""), body, "ok")
    if isinstance(res, dict) and res.get("ok"):
        return ok(res)
    reason = res.get("reason", "信号路由失败") if isinstance(res, dict) else "信号路由失败"
    return err(400, reason, res)


# ---------------- 目标持仓差量同步 ----------------

