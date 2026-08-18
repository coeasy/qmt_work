"""后端冒烟测试（真实券商模式，无 mock）。

验证点：
- 服务可启动、MCP/REST/静态同源可用
- 未连接券商时，行情/交易/账户/回测端点返回明确的 503（绝不返回假数据）
- 券商管理端点可用（profiles / 列表 / 新增校验 / 探测）
- LLM 配置加密、API Key 管理、Agent 未配置提示等基础功能正常
- WebSocket 可连接并收到快照

用法：先启动服务（python run.py），再 python tests/smoke2.py
"""
import asyncio
import json
import os
import sqlite3
import websockets

BASE = os.environ.get("QMT_TEST_BASE", "http://127.0.0.1:21118/api/v1")
WS_BASE = BASE.replace("http://", "ws://").rstrip("/") + "/ws"
# 默认指向源码库；验收打包 EXE 时通过 QMT_DB_PATH 指向其同目录库（reset 才生效）
DB_PATH = os.environ.get("QMT_DB_PATH", "data/app.db")

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


def reset_llm_config():
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute("DELETE FROM llm_config")
        c.commit(); c.close()
    except Exception:
        pass


async def get(path):
    import httpx
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(BASE + path)
        return r.status_code, r.json()


async def post(path, body=None):
    import httpx
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(BASE + path, json=body or {})
        return r.status_code, r.json()


async def put(path, body=None):
    import httpx
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.put(BASE + path, json=body or {})
        return r.status_code, r.json()


async def delete(path):
    import httpx
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.delete(BASE + path)
        return r.status_code, r.json()


def reset_llm():
    reset_llm_config()


async def test_broker_profiles():
    code, data = await get("/brokers/profiles")
    ok = data.get("code") == 0 and len(data.get("data", [])) >= 6
    names = [p["id"] for p in data.get("data", [])]
    check("broker profiles 列表", ok, f"{code} {names}")
    check("包含国金/华鑫/银河/同花顺/恒生/掘金",
          all(x in names for x in ["guojin", "huaxin", "yinhe", "ths", "ptrade", "juejin"]))


async def test_brokers_list():
    code, data = await get("/brokers")
    check("brokers 列表可访问", data.get("code") == 0, str(code))


async def test_add_broker_invalid():
    code, data = await post("/brokers", {"broker_id": "nope", "account_id": "123"})
    check("新增未知券商 -> code=400", data.get("code") == 400,
          f"{code} {data.get('message')}")


async def test_add_broker_unknown_client():
    # 提供合法 broker_id 但不存在的客户端路径：应返回探测失败（503）而非假数据
    code, data = await post("/brokers", {
        "broker_id": "guojin", "account_id": "55012345",
        "client_path": "C:/no_such_qmt/userdata_mini", "autoconnect": True})
    check("新增合法券商（客户端不存在）返回探测结果", data.get("code") == 0,
          f"{code} {data}")


async def test_account_no_broker():
    code, data = await get("/account/status")
    check("未连接券商 -> 账户端点 code=503", data.get("code") == 503, f"{code} {data}")
    check("503 提示含「券商连接」", "券商" in (data.get("message") or ""), str(data.get("message")))


async def test_market_crawl_no_broker():
    code, data = await post("/market/crawl", {"codes": ["600519.SH"], "days": 5})
    check("未连接券商 -> 行情爬取 code=503", data.get("code") == 503, f"{code} {data}")


async def test_backtest_no_broker():
    code, data = await post("/backtest/jobs", {"kind": "backtest", "params": {
        "symbol": "600519.SH", "strategy": "ma_cross", "count": 250}})
    check("回测任务可提交", data.get("code") == 0 and "id" in data.get("data", {}), f"{code} {data}")
    job_id = (data.get("data") or {}).get("id")
    if job_id:
        for _ in range(20):
            _, j = await get(f"/backtest/jobs/{job_id}")
            st = (j.get("data") or {}).get("status")
            if st in ("done", "failed"):
                break
            await asyncio.sleep(0.5)
        # 无券商时作业应优雅失败（status=failed），而非崩溃
        _, j = await get(f"/backtest/jobs/{job_id}")
        check("无券商回测优雅失败", (j.get("data") or {}).get("status") == "failed",
              f"{j.get('data')}")


async def test_llm_config():
    reset_llm()
    code, data = await get("/config/llm")
    check("LLM 未配置态", data.get("code") == 0 and data["data"]["configured"] is False, str(code))
    code, data = await put("/config/llm", {
        "provider": "openai", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o", "api_key": "sk-test-1234567890"})
    check("LLM 配置保存", data.get("code") == 0, str(code))
    code, data = await get("/config/llm")
    masked = data["data"].get("api_key_masked", "")
    check("LLM API Key 脱敏", masked == "sk-********7890", f"masked={masked}")


async def test_api_keys():
    code, data = await post("/api-keys", {"name": "test", "scopes": "trade,quote"})
    check("API Key 创建", data.get("code") == 0 and "api_key" in data.get("data", {}), str(code))
    code, data = await get("/api-keys")
    check("API Key 列表", data.get("code") == 0 and len(data.get("data", [])) >= 1, str(code))


async def test_agent():
    reset_llm()
    import httpx
    async with httpx.AsyncClient(timeout=15) as cli:
        async with cli.stream("POST", BASE + "/agent/chat", json={"message": "hi"}) as r:
            body = ""
            async for line in r.aiter_lines():
                body += line
    check("Agent 未配置 LLM 提示", "Agent 未配置" in body, body[:120])


async def test_ws():
    try:
        async with websockets.connect(WS_BASE) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            obj = json.loads(msg)
            check("WS 连接并收到快照", obj.get("type") == "snapshot", str(obj)[:80])
            await ws.send(json.dumps({"action": "ping"}))
            pong = await asyncio.wait_for(ws.recv(), timeout=5)
            check("WS ping/pong", "pong" in pong, pong[:80])
    except Exception as exc:
        check("WS 连接", False, str(exc))


async def test_ws_reconnect():
    """阶段 4：断线重连——断线后重连应收到新的快照。"""
    try:
        async with websockets.connect(WS_BASE) as ws:
            msg1 = await asyncio.wait_for(ws.recv(), timeout=5)
            obj1 = json.loads(msg1)
            check("WS 首次连接收到快照", obj1.get("type") == "snapshot", str(obj1)[:80])
    except Exception as exc:
        check("WS 首次连接", False, str(exc))
        return
    # 断线（自动随 with 块退出）& 重连
    try:
        async with websockets.connect(WS_BASE) as ws2:
            msg2 = await asyncio.wait_for(ws2.recv(), timeout=5)
            obj2 = json.loads(msg2)
            check("WS 断线重连后收到新快照", obj2.get("type") == "snapshot", str(obj2)[:80])
    except Exception as exc:
        check("WS 断线重连", False, str(exc))


async def test_risk_circuit_breaker():
    """阶段 4：风控熔断拦截——手动熔断后提交信号被拦截，解除后恢复。"""
    # 先解除熔断确保干净状态
    await post("/config/risk/circuit", {"action": "reset"})
    # 手动触发熔断
    code, data = await post("/config/risk/circuit", {"action": "trip", "reason": "smoke2 熔断测试"})
    check("熔断触发返回 code=0", data.get("code") == 0, f"{code} {data}")
    check("熔断状态为 true", data.get("data", {}).get("circuit_broken") is True, str(data))
    # 提交信号：应在风险检查阶段被拦截，返回 503
    code, data = await post("/signal/submit", {
        "source": "test", "code": "600519.SH", "side": "buy",
        "volume": 100, "price": 10.0, "price_type": "limit"})
    check("熔断期间信号被拦",
          data.get("code") == 503 and "熔断" in str(data), f"{code} {data}")
    # 解除熔断
    code, data = await post("/config/risk/circuit", {"action": "reset"})
    check("熔断解除返回 code=0", data.get("code") == 0, f"{code} {data}")
    check("熔断状态为 false", data.get("data", {}).get("circuit_broken") is False, str(data))


async def test_idempotent_concurrent():
    """阶段 4：幂等并发——同一 idempotency_key 并发提交只执行一次真实下单（paper 模式）。"""
    import time
    import httpx

    # 记录并切换到 paper 模式（无需券商连接即可端到端验证幂等）
    _, mode_data = await get("/signal/mode")
    prev_mode = (mode_data.get("data") or {}).get("mode", "live")
    await post("/signal/mode", {"mode": "paper"})
    tag = f"smoke-idem-{int(time.time() * 1000)}"
    key = f"order:{tag}"
    signal = {"source": "test", "code": "600519.SH", "side": "buy",
              "volume": 100, "price": 10.0, "price_type": "limit",
              "remark": tag, "idempotency_key": key}
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            tasks = [cli.post(BASE + "/signal/submit", json=signal) for _ in range(5)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
        results = [r.json().get("data", {}) for r in responses
                   if isinstance(r, httpx.Response) and r.status_code == 200]
        check("并发请求全部返回 HTTP 200", len(results) == 5, f"got {len(results)} / 5")
        # 恰好一个真实执行（无 duplicated），其余标记重复（幂等命中）
        real = [d for d in results if not d.get("duplicated")]
        dup = [d for d in results if d.get("duplicated")]
        check("仅一次真实执行，其余标记重复",
              len(real) == 1 and len(dup) == 4,
              f"real={len(real)} dup={len(dup)}")
        # 数据库仅落一条 paper 订单（同一 idempotency_key 只执行一次）
        try:
            c = sqlite3.connect(DB_PATH)
            n = c.execute("SELECT COUNT(*) FROM paper_orders WHERE remark=?",
                          (tag,)).fetchone()[0]
            c.close()
        except Exception:
            n = -1
        check("paper_orders 仅落一条记录", n == 1, f"rows={n}")
    finally:
        await post("/signal/mode", {"mode": prev_mode})


async def test_mcp_handshake():
    """MCP Streamable HTTP 协议验证：POST /mcp/ initialize 握手。"""
    import httpx
    mcp_url = BASE.replace("/api/v1", "/mcp/")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "smoke2", "version": "1.0"}}}
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.post(
            mcp_url, json=payload,
            headers={"Accept": "application/json, text/event-stream",
                     "Content-Type": "application/json"})
    body = r.text
    check("MCP POST /mcp/ 返回 200", r.status_code == 200, str(r.status_code))
    check("MCP initialize 含 protocolVersion", "protocolVersion" in body, body[:160])
    check("MCP initialize 含 capabilities", "capabilities" in body, body[:160])
    # GET /mcp/ 无 Accept 时按协议返回 4xx（非 500），属正常行为
    async with httpx.AsyncClient(timeout=10) as cli:
        g = await cli.get(mcp_url)
    check("MCP GET /mcp/ 返回 4xx（协议正常）", 400 <= g.status_code < 500, str(g.status_code))


async def test_ready():
    """就绪探针：/api/v1/ready 应返回 HTTP 200 且 code=0（非就绪为 503）。"""
    code, data = await get("/ready")
    check("ready 探针 HTTP 200 + code=0",
          code == 200 and data.get("code") == 0, f"{code} {str(data)[:160]}")


async def main():
    print("=== smoke2 (real-broker mode) ===")
    await test_broker_profiles()
    await test_brokers_list()
    await test_add_broker_invalid()
    await test_add_broker_unknown_client()
    await test_account_no_broker()
    await test_market_crawl_no_broker()
    await test_backtest_no_broker()
    await test_llm_config()
    await test_api_keys()
    await test_agent()
    await test_ws()
    await test_ws_reconnect()
    await test_risk_circuit_breaker()
    await test_idempotent_concurrent()
    await test_mcp_handshake()
    await test_ready()
    print(f"\n=== RESULT: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    import sys
    rc = asyncio.run(main())
    sys.exit(1 if rc else 0)
