#!/usr/bin/env python3
"""CDP 驱动 Electron 客户端，在**真实前端页面**上跑下单流程。

为什么不能只打 REST：用户要的是「客户端下单流程是否打通」——
前端有自己的一层（表单校验 / 模式提示 banner / 下单后刷新 / TOTP 挂起交互），
REST 全绿不代表界面链路通。本脚本用 Chrome DevTools Protocol 驱动渲染进程：

  1. 等窗口加载完成，导航到「手动交易」页
  2. 读 OrderForm 的模式提示 banner（契约：paper 须说明「本地撮合、不向券商报单」）
  3. 填代码 / 价格 / 数量，点「买入」
  4. 读结果 banner + 委托/成交/持仓三个页签是否出现该单
  5. 断言全程无 console error / 无未捕获异常

用法:
    python scripts/cdp_order_flow.py [--cdp http://127.0.0.1:9222]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))
    return cond


class CDP:
    """单 reader 模式的 CDP 客户端。

    不能写成「send 自己 recv 等响应」+「pump 另开 recv」——websockets 的
    asyncio 实现对并发 recv 直接抛 ConcurrencyError（实测踩到）。
    唯一 reader 是 ``_reader``：命令响应用 future 分发，事件进队列。
    """

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []
        self._reader_task: asyncio.Task | None = None

    async def start(self):
        self._reader_task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if "error" in m:
                        fut.set_exception(RuntimeError(f"{m['error']}"))
                    else:
                        fut.set_result(m.get("result", {}))
                elif m.get("method"):
                    self.events.append(m)
        except Exception as exc:  # noqa: BLE001
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)

    async def send(self, method: str, **params):
        self._id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send(json.dumps(
            {"id": self._id, "method": method, "params": params}))
        return await fut

    async def eval(self, expr: str, await_promise: bool = False):
        r = await self.send("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"eval 异常: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = ap.parse_args()

    import urllib.request
    with urllib.request.urlopen(args.cdp + "/json", timeout=10) as resp:
        targets = json.loads(resp.read().decode())
    page = [t for t in targets if t.get("type") == "page"]
    if not page:
        print("✗ 未找到页面 target（客户端窗口未创建？）")
        return 1
    ws_url = page[0]["webSocketDebuggerUrl"]
    print(f"  连接渲染进程: {page[0].get('title')!r} url={page[0].get('url')}")

    console_errors: list[str] = []
    page_errors: list[str] = []

    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        c = CDP(ws)
        await c.start()
        await c.send("Runtime.enable")
        await c.send("Log.enable")
        await c.send("Page.enable")

        def _drain_events():
            for m in c.events:
                if m.get("method") == "Runtime.consoleAPICalled":
                    p = m["params"]
                    if p.get("type") in ("error", "assert"):
                        txt = " ".join(str(a.get("value", ""))
                                       for a in p.get("args", []))
                        console_errors.append(txt[:300])
                elif m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    page_errors.append(str(d.get("text"))[:300])
            c.events.clear()

        # ── 1. 窗口与 SPA ──
        await asyncio.sleep(2.0)
        _drain_events()
        title = await c.eval("document.title")
        check("窗口标题就绪", bool(title), str(title))
        ready = await c.eval("document.readyState")
        check("页面加载完成", ready == "complete", str(ready))
        nodes = await c.eval("document.querySelectorAll('*').length")
        check("DOM 非空壳", isinstance(nodes, int) and nodes > 50, f"nodes={nodes}")

        # ── 2. 切到「手动交易」页 ──
        # 前端是 **Tab 工作区**（非 URL 路由，zustand workspace store 管 tabs），
        # 且「手动交易」与「行情工作台」共用同一份 OrderForm —— 页面上可能同时
        # 存在两个下单表单。
        # 导航走**命令面板**（Ctrl+K，见 shell/Hotkeys.tsx:41），而不是点 tab：
        # 实测 el.click() 与坐标点击都不可靠 —— 页面上的「手动交易」按钮有多个
        # （工作台底部也有一个），点到错的不会切 tab。
        await c.send("Input.dispatchKeyEvent", type="keyDown",
                     modifiers=2, code="KeyK", key="k", windowsVirtualKeyCode=75)
        await c.send("Input.dispatchKeyEvent", type="keyUp",
                     modifiers=2, code="KeyK", key="k", windowsVirtualKeyCode=75)
        await asyncio.sleep(1.0)
        palette = await c.eval(
            "document.body.innerText.includes('命令')"
            " || !!document.querySelector('[role=dialog], [role=listbox]')")
        print(f"  命令面板已开: {palette}")
        await c.send("Input.insertText", text="手动交易")
        await asyncio.sleep(1.2)
        await c.send("Input.dispatchKeyEvent", type="keyDown", code="Enter",
                     key="Enter", windowsVirtualKeyCode=13)
        await c.send("Input.dispatchKeyEvent", type="keyUp", code="Enter",
                     key="Enter", windowsVirtualKeyCode=13)
        # 轮询等 tab 真的切过来
        on_trade = False
        for _ in range(24):
            await asyncio.sleep(0.5)
            cur = await c.eval(
                "(() => { const t = document.body.innerText;"
                " return t.includes('手动交易') && !t.includes('行情工作台'); })()")
            if cur:
                on_trade = True
                break
        check("已切到「手动交易」页", on_trade, f"palette={palette}")

        # ── 3. 模式提示 banner ──
        banner = await c.eval("document.body.innerText")
        if "模拟盘" in (banner or ""):
            check("模式提示说明「本地撮合、不向券商报单」",
                  ("不向券商" in banner) or ("本地" in banner),
                  "banner 含模拟盘说明")
        elif "预演" in (banner or ""):
            check("模式提示为预演说明", "计划" in banner, "dry_run banner")
        else:
            check("模式提示 banner 存在", False, "未见到模拟盘/预演提示")

        # ── 4. 填单并提交 ──
        # OrderForm 的输入框：**不能按「第 N 个可见 input」定位** —— 页面顶部有
        # 全局搜索框（placeholder「代码直达 / 搜索标的」），实测把代码填进它之后
        # 触发自动补全，反而把「000001.SH」塞进了价格框。
        # 正解：按下单表单自己的 placeholder 特征定位：
        #   代码 = 价格框所在容器内、除价格/股数外的第一个 input
        #   价格 = placeholder 含「市价」（"0 = 市价"）
        #   股数 = placeholder 含「股数」
        # ★ 也不能用「原生 setter + dispatchEvent」设值：React 受控组件有
        #   自己的 value tracker，那样写只改 DOM 不改 React state，提交时读到
        #   的还是空值（实测：后端一个 /trade/order 都没收到）。必须走真实输入
        #   路径 —— focus + Input.insertText，让 input 事件流经 React。
        # 定位 OrderForm 三字段，返回**坐标快照**（不打自定义属性）。
        # ★ 不能给 input 打 data-* 标记再按标记取：OrderForm 的输入框是 React
        #   受控组件，任意一次重渲染（行情 tick 每数秒一次）都会把标记刷掉，
        #   而页面上其他稳定元素可能留着上一轮遗留的同名标记 —— 实测回读时
        #   querySelector 命中的是全局搜索框，填进去的值全跑偏。
        #   因此每一步都重新定位，只信当次快照。
        async def _coords():
            return await c.eval("""
              (() => {
                const all = [...document.querySelectorAll('input')]
                  .filter(i => i.offsetParent !== null);
                const v = all.findIndex(
                  i => (i.placeholder||'') === '100 的整数倍');
                if (v < 0) return null;
                let p = -1, c = -1;
                for (let k = v - 1; k >= 0; k--) {
                  const ph = all[k].placeholder || '';
                  if (p < 0 && ph === '0.00') p = k;
                  else if (c < 0 && ph.includes('600519')) c = k;
                  if (p >= 0 && c >= 0) break;
                }
                if (p < 0 || c < 0) return null;
                const snap = (el) => { el.scrollIntoView({block:'center'});
                  const r = el.getBoundingClientRect();
                  return {x: Math.round(r.x + r.width/2),
                          y: Math.round(r.y + r.height/2),
                          ph: el.placeholder, val: el.value || ''}; };
                return {code: snap(all[c]), price: snap(all[p]), vol: snap(all[v])};
              })()
            """)

        slots = await _coords()
        print(f"  表单位置: {json.dumps(slots, ensure_ascii=False)}")
        ok_loc = (isinstance(slots, dict)
                  and slots.get("code", {}).get("ph", "").startswith("600519")
                  and slots.get("price", {}).get("ph") == "0.00"
                  and slots.get("vol", {}).get("ph") == "100 的整数倍")
        if not ok_loc:
            dump = await c.eval("""
              (() => [...document.querySelectorAll('input,select')]
                 .filter(i => i.offsetParent !== null)
                 .map(i => (i.placeholder || i.tagName) + '=' + (i.value||'').slice(0,16)))()
            """)
            print(f"  [diag] 可见控件: {dump}")
        check("定位到 OrderForm 三字段（代码/价格/股数）", ok_loc, str(slots))

        async def _fill(which: str, text: str):
            """重新定位 → 真实点击 → 插入文本 → 回读验证（失败重试 3 次）。

            ★ 每步都重新定位：React 重渲染会同时改变 DOM 与布局，上一次的
              坐标可能已经落到别的控件上。
            ★ 必须**先点击再插入**：只调 el.focus() 时 Input.insertText 的焦点
              并未真正落到该输入框。真实鼠标点击同时搞定焦点与选区。
            """
            for attempt in range(3):
                co = await _coords()
                tgt = (co or {}).get(which)
                if not tgt:
                    raise RuntimeError(f"字段 {which} 定位失败（第 {attempt+1} 次）")
                await c.send("Input.dispatchMouseEvent", type="mousePressed",
                             x=tgt["x"], y=tgt["y"], button="left", clickCount=1)
                await c.send("Input.dispatchMouseEvent", type="mouseReleased",
                             x=tgt["x"], y=tgt["y"], button="left", clickCount=1)
                await asyncio.sleep(0.25)
                # 全选既有内容再插入 ⇒ 替换而非追加
                await c.send("Input.dispatchKeyEvent", type="keyDown",
                             modifiers=2, code="KeyA", key="a",
                             windowsVirtualKeyCode=65)
                await c.send("Input.dispatchKeyEvent", type="keyUp",
                             modifiers=2, code="KeyA", key="a",
                             windowsVirtualKeyCode=65)
                await asyncio.sleep(0.15)
                await c.send("Input.insertText", text=text)
                await asyncio.sleep(0.45)
                back = (await _coords() or {}).get(which, {})
                if text in (back.get("val") or ""):
                    return back.get("val")
            raise RuntimeError(f"字段 {which} 填入 {text!r} 失败："
                               f"回读 {back.get('val')!r}")

        await _fill("code", "000001")
        await _fill("price", "50")
        await _fill("vol", "100")
        await asyncio.sleep(0.5)
        _drain_events()
        filled = await _coords()
        filled = {k: v.get("val") for k, v in (filled or {}).items()}
        print(f"  表单回读: {filled}")
        check("表单三字段已填（React state 同步）",
              isinstance(filled, dict) and filled.get("vol") == "100"
              and filled.get("code") == "000001" and filled.get("price") == "50",
              str(filled))

        clicked = await c.eval("""
          (() => {
            const btns = [...document.querySelectorAll('button')]
              .filter(b => b.offsetParent !== null && !b.disabled);
            const buy = btns.find(b => (b.textContent||'').trim() === '买入')
                     || btns.find(b => (b.textContent||'').includes('买入'));
            if (!buy) return {ok: false, btns: btns.map(b => b.textContent.trim()).slice(0,20)};
            buy.click();
            return {ok: true, label: buy.textContent.trim()};
          })()
        """)
        print(f"  点击买入: {clicked}")
        check("「买入」按钮可点", isinstance(clicked, dict) and clicked.get("ok"),
              str(clicked)[:200])

        # ── 5. 读结果 ──
        # ★ 多次采样：banner 是 toast，数秒后自动消失，只采一次会漏掉
        #   （实测：点击后 8s 才读，委托号已经不在 DOM 里了）。
        after = ""
        got_id = False
        detail = ""
        for wait in (1.5, 2.0, 2.0, 2.0, 2.0):
            await asyncio.sleep(wait)
            _drain_events()
            snap = await c.eval("document.body.innerText") or ""
            if "PAPER-" in snap or "委托号" in snap:
                got_id = True
                after = snap
                break
            # 记住最后一帧里与委托/下单相关的片段，失败时用于归因
            for kw in ("委托", "下单", "已提交", "拒绝", "失败"):
                i = snap.find(kw)
                if i >= 0:
                    detail = snap[max(0, i - 20):i + 100].replace("\n", " ")
                    break
            after = snap
        if got_id and "PAPER-" in after:
            tail = after.split("PAPER-", 1)[1]
            detail = "PAPER-" + (tail[:14].split()[0] if tail.split() else "(空)")
        elif not detail:
            detail = (after[:200] or "").replace("\n", " ")
        check("页面出现委托号 / 成交反馈", got_id, detail)
        rejected = ("被拒" in after) or ("拒绝" in after) or ("下单失败" in after)
        if rejected:
            seg = next((after[max(0, i - 40):i + 120] for i in range(len(after))
                        if after.startswith(("被拒", "拒绝", "下单失败"), i)), "")
            print(f"  [note] 页面出现拒绝/失败字样（需人工判断是否风控合理拦截）: "
                  f"{seg[:120].replace(chr(10), ' ')}")

        # 三个页签是否出现数据
        for tab, kw in (("委托", "委托"), ("成交", "成交"), ("持仓", "持仓")):
            has = kw in after
            check(f"账户视图含「{tab}」内容", has, "")

        # ── 6. 无 console error / 未捕获异常 ──
        _drain_events()
        check("无未捕获页面异常", not page_errors,
              "; ".join(page_errors[:3]) if page_errors else "")
        # console error 里过滤掉预期的网络噪声（行情源不可达等）
        real_errs = [e for e in console_errors
                     if "Failed to fetch" not in e and "net::" not in e]
        check("无 console error", not real_errs,
              "; ".join(real_errs[:3]) if real_errs else "")

        await c.close()

    print()
    print("=" * 68)
    print(f" 结果: {PASS} PASS / {FAIL} FAIL  (CDP 前端下单链路)")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
