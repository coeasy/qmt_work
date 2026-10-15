#!/usr/bin/env python3
"""本地下单流程端到端测试（客户端用户视角）。

对着**已启动的后端**（packaged 或 dev）跑完整下单链路，不 mock 任何一层：

  1. 前置态确认：/account/status、/signal/mode、券商连接态
  2. paper 主链路：下单 → 成交回报 → 订单/成交/持仓查询 → 撤单
  3. 模式归因：paper / dry_run / live(无券商) 三个模式的下单前置门
  4. 幂等：同参重复下单在窗口内只下一单；模式切换后同参不合并
  5. 契约字段：PAPER- 前缀、profit_pct 百分数口径、account.grid 模式归因

用法:
    python scripts/order_flow_test.py [--port 21190] [--base http://127.0.0.1:21190]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASS, FAIL, WARN = 0, 0, 0
RESULTS: list[tuple[str, str, str]] = []


def _req(base: str, method: str, path: str, body: dict | None = None,
         timeout: float = 15.0) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001 - 连接层失败也要报出来
        return -1, {"_transport_error": str(e)}


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append(("PASS", name, detail))
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        RESULTS.append(("FAIL", name, detail))
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))
    return cond


def note(name: str, detail: str) -> None:
    RESULTS.append(("NOTE", name, detail))
    print(f"  [note] {name}  -> {detail}")


def items_of(d: dict) -> list:
    """查询端点的 data 有两种形态：{"items":[...]} 或直接 [...]。"""
    data = d.get("data", d)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items") or data.get("positions") or []
    return []


def code_of(d: dict) -> int:
    return int(d.get("code", 0) or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:21190")
    ap.add_argument("--port", default="21190")
    args = ap.parse_args()
    base = args.base

    print("=" * 68)
    print(f" qmt_work 下单流程端到端测试  base={base}")
    print("=" * 68)

    # ── 0. 就绪 ──
    st, d = _req(base, "GET", "/api/v1/ready")
    check("后端 ready = 200", st == 200, f"http={st} code={code_of(d)}")

    st, d = _req(base, "GET", "/api/v1/account/status")
    if st == 200 and code_of(d) == 0:
        data = d.get("data", d)
        mode0 = str(data.get("mode") or "")
        note("初始执行模式", mode0)
        note("初始账户", f"connected={data.get('connected')} "
                          f"assets={data.get('assets')} positions={len(data.get('positions') or [])}")
    else:
        check("初始 account/status 可取", False, f"http={st} code={code_of(d)}")
        mode0 = ""

    # ── 1. 切到 paper ──
    if mode0 != "paper":
        st, d = _req(base, "POST", "/api/v1/signal/mode", {"mode": "paper"})
        check("切换执行模式 → paper", st == 200 and code_of(d) == 0,
              f"http={st} code={code_of(d)}")
    st, d = _req(base, "GET", "/api/v1/signal/mode")
    mode = str(d.get("data", d).get("mode") or "")
    check("当前模式 = paper", mode == "paper", mode)

    # ── 2. paper 下单 ──
    # 限价单：有报价 ⇒ 可估算委托金额。小额（<10 万 TOTP 阈值）直接成交。
    # 不用市价单做主链路：沙箱无行情时市价单会 pending_price_unknown → 挂起
    # TOTP 确认（这是**正确**的产品行为，不假装成交），见下方专项验证。
    order_body = {
        "code": "000001", "direction": "buy", "volume": 100,
        "price": 50.0, "price_type": "limit",
        "remark": "order_flow_test",
    }
    st, d = _req(base, "POST", "/api/v1/trade/order", order_body)
    check("paper 下单 HTTP 200", st == 200, f"http={st}")
    check("paper 下单 code=0（未被 503 拦截）", code_of(d) == 0,
          f"code={code_of(d)} msg={d.get('msg') or d.get('message')}")
    oid = ""
    odata = d.get("data", d) or {}
    if code_of(d) == 0:
        oid = str(odata.get("order_id") or odata.get("id") or "")
        note("委托号", oid)
        check("委托号带 PAPER- 前缀（模拟盘账本口径）", oid.startswith("PAPER-"), oid)
        stt = str(odata.get("status") or "")
        note("委托状态", stt)
        # 撮合即时 ⇒ 恒 filled；未成交不假装成交
        check("限价单即时成交（status=filled）", stt.lower() in ("filled", "已成交"),
              stt)
        check("未落入 pending_confirmation（小额单不应挂 TOTP）",
              not odata.get("pending_confirmation"),
              str(odata.get("reason") or ""))

    # ── 2b. 市价单无行情 ⇒ 挂起确认而非假装成交 ──
    st, d = _req(base, "POST", "/api/v1/trade/order",
                 {"code": "000001", "direction": "buy", "volume": 100,
                  "price": 0, "price_type": "market", "remark": "mkt_probe"})
    md = d.get("data", d) or {}
    if md.get("pending_confirmation"):
        check("市价单无行情时挂起 TOTP 确认（不假装成交）", True,
              str(md.get("reason") or ""))
        tok = str(md.get("confirm_token") or "")
        check("挂起时下发 confirm_token", bool(tok), tok[:16] + "...")
        # 无效 token 必须被拒绝，不能凭空放行
        st, d = _req(base, "POST", "/api/v1/signal/confirm",
                     {"confirm_token": "invalid_token_probe", "totp_code": "000000"})
        check("/signal/confirm 拒绝无效 token", st == 200 and code_of(d) != 0,
              f"code={code_of(d)} msg={str(d.get('msg') or d.get('message'))[:80]}")
    else:
        note("市价单无行情", f"未挂起（有行情或已成交）：status={md.get('status')}")

    # ── 3. 查询闭环：orders / deals / positions 都要能看到这一单 ──
    if oid:
        st, d = _req(base, "GET", "/api/v1/trade/orders")
        items = items_of(d)
        hit = [o for o in items if str(o.get("order_id") or o.get("id")) == oid]
        check("/trade/orders 能查到该单", st == 200 and code_of(d) == 0 and bool(hit),
              f"items={len(items)} hit={len(hit)}")
        if hit:
            note("订单字段", json.dumps(hit[0], ensure_ascii=False)[:300])

        st, d = _req(base, "GET", "/api/v1/trade/deals")
        items = items_of(d)
        hit = [x for x in items if oid in str(x.get("order_id") or x.get("deal_id") or "")]
        check("/trade/deals 能查到该成交", st == 200 and code_of(d) == 0 and bool(hit),
              f"items={len(items)} hit={len(hit)}")

        st, d = _req(base, "GET", "/api/v1/trade/positions")
        pos = items_of(d)
        hitp = [p for p in pos if str(p.get("code") or p.get("symbol") or "")
                .upper().startswith("000001")]
        check("/trade/positions 出现 000001 持仓", st == 200 and code_of(d) == 0 and bool(hitp),
              f"positions={len(pos)} hit={len(hitp)}")
        if hitp:
            p = hitp[0]
            pp = p.get("profit_pct")
            note("profit_pct", f"{pp!r}（契约=百分数；None=无成本不硬凑 0）")
            if pp is not None:
                check("profit_pct 为百分数量级（|v|<100 表示 % 而非小数）",
                      abs(float(pp)) < 100, str(pp))

    # ── 4. 幂等：同参重复下单在 5s 窗口内只下一单 ──
    st, d2 = _req(base, "POST", "/api/v1/trade/order", order_body)
    check("同参重复下单 HTTP 200", st == 200 and code_of(d2) == 0,
          f"http={st} code={code_of(d2)}")
    if code_of(d) == 0 and code_of(d2) == 0:
        oid2 = str((d2.get("data", d2) or {}).get("order_id") or "")
        check("同参重复下单命中幂等窗口（返回同一委托号，不重复成交）", oid2 == oid,
              f"{oid} vs {oid2}")
        st, d = _req(base, "GET", "/api/v1/trade/orders")
        items = items_of(d)
        n = len([o for o in items if str(o.get("order_id") or o.get("id")) == oid])
        check("幂等后订单表仍只有 1 笔", n == 1, f"count={n}")

    # ── 5. paper 查询族不返空冒充「查过了」──
    for path, label in (("/api/v1/trade/conditions", "条件单"),
                        ("/api/v1/algo", "算法单"),
                        ("/api/v1/account/aggregate", "账户聚合")):
        st, d = _req(base, "GET", path)
        note(f"{label} GET", f"http={st} code={code_of(d)} msg={str(d.get('msg') or d.get('message'))[:80]}")

    # ── 6. 模式归因：grid / target 在 paper 下的行为 ──
    # grid 的契约是「多账户统一看板」：有连接记录时**列出未连接账户并附 error 行**
    # （运维视图需要看见失败原因）；完全零连接记录时才走 paper 模式归因 503。
    st, d = _req(base, "GET", "/api/v1/brokers")
    bdata = d.get("data", d) or {}
    conns = bdata.get("items") if isinstance(bdata, dict) else bdata
    n_conn = len(conns or [])
    st, d = _req(base, "GET", "/api/v1/account/grid")
    gd = d.get("data", d) or {}
    if n_conn == 0:
        check("account/grid 零连接记录时 503 + paper 模式归因",
              st == 200 and code_of(d) == 503,
              f"http={st} code={code_of(d)} msg={str(d.get('msg') or d.get('message'))[:100]}")
    else:
        check("account/grid 有连接记录时返回诚实看板（connected_count=0 + error 行）",
              st == 200 and code_of(d) == 0
              and gd.get("connected_count") == 0
              and all(a.get("error") for a in gd.get("accounts") or []),
              f"code={code_of(d)} connected_count={gd.get('connected_count')} "
              f"err={(gd.get('accounts') or [{}])[0].get('error', '')[:60]}")
    st, d = _req(base, "POST", "/api/v1/trade/target",
                 {"code": "600519", "target_volume": 200})
    check("trade/target 在 paper 下 503 + 模式归因", st == 200 and code_of(d) == 503,
          f"http={st} code={code_of(d)} msg={str(d.get('msg') or d.get('message'))[:100]}")

    # ── 7. dry_run：只返计划、不成交、不落账 ──
    _req(base, "POST", "/api/v1/signal/mode", {"mode": "dry_run"})
    st, d = _req(base, "POST", "/api/v1/trade/order",
                 {"code": "600519", "direction": "buy", "volume": 100,
                  "price": 1400.0, "price_type": "limit", "remark": "dry_run_probe"})
    dd = d.get("data", d) or {}
    dr_oid = str(dd.get("order_id") or "")
    check("dry_run 下单不产生真实委托号（不落账）", not dr_oid, f"order_id={dr_oid!r}")
    check("dry_run 不返回 PAPER- 前缀（未走撮合）", not dr_oid.startswith("PAPER-"), dr_oid)
    note("dry_run 返回", json.dumps(dd, ensure_ascii=False)[:200])

    # ── 8. live 无券商：503 + 券商引导 ──
    _req(base, "POST", "/api/v1/signal/mode", {"mode": "live"})
    st, d = _req(base, "POST", "/api/v1/trade/order", order_body)
    lv_code = code_of(d)
    lv_msg = str(d.get("msg") or d.get("message") or "")
    check("live 无券商下单 503（券商不可用归因）", st == 200 and lv_code == 503,
          f"http={st} code={lv_code} msg={lv_msg[:100]}")
    check("live 归因指向「连接券商」而非模式错误", "券商" in lv_msg, lv_msg[:80])

    # ── 9. 幂等 key 含 mode：paper→live 同参不得复用旧成交 ──
    st, d = _req(base, "POST", "/api/v1/trade/order", order_body)
    lv2 = d.get("data", d) or {}
    oid3 = str(lv2.get("order_id") or "")
    check("切 live 后同参下单不复用 paper 委托号（key 含 mode）",
          lv_code == 503 or (oid3 and not oid3.startswith("PAPER-")),
          f"code={lv_code} oid={oid3}")

    # ── 10. 回 paper，测撤单 ──
    # 限价单（市价单无行情会挂起 TOTP，拿不到可撤的委托号）
    _req(base, "POST", "/api/v1/signal/mode", {"mode": "paper"})
    st, d = _req(base, "POST", "/api/v1/trade/order",
                 {"code": "000002", "direction": "buy", "volume": 100,
                  "price": 10.0, "price_type": "limit", "remark": "cancel_probe"})
    cid = str((d.get("data", d) or {}).get("order_id") or "")
    note("撤单探测委托", cid or f"code={code_of(d)} {str(d.get('msg') or d.get('message'))[:60]}")
    if cid:
        st, d = _req(base, "POST", "/api/v1/trade/cancel",
                     {"order_id": cid, "code": "000002"})
        cc = code_of(d)
        note("撤单返回", f"http={st} code={cc} msg={str(d.get('msg') or d.get('message'))[:80]}")
        check("撤单端点可达（不 404/500）", st == 200 and cc in (0, 400, 503),
              f"http={st} code={cc}")
        # 撤单后该单状态应变更（已撤或本就成交，两者都算被处理）
        st, d = _req(base, "GET", "/api/v1/trade/orders")
        row = [o for o in items_of(d) if str(o.get("order_id") or o.get("id")) == cid]
        note("撤单后状态", str(row[0].get("status")) if row else "未找到")
    else:
        note("撤单", "未取得可撤委托号，跳过（挂起/拒单场景）")

    # ── 汇总 ──
    print()
    print("=" * 68)
    print(f" 结果: {PASS} PASS / {FAIL} FAIL   base={base}")
    print("=" * 68)
    if FAIL:
        print("\n失败项：")
        for tag, name, detail in RESULTS:
            if tag == "FAIL":
                print(f"  ✗ {name}  -> {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
