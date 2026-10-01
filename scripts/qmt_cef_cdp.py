#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大 QMT 内置 Chromium（CEF）远程调试客户端 —— 用 CDP 驱动 QMT 的**网页型**面板。

为什么会有这个工具（2026-10-01 真机实测）
------------------------------------------
大 QMT 的主界面是原生 Qt，但它的若干面板（帮助页 `/innerApi/*`、AI 策略编辑器
`/webstrategyedit/index.html`、`webwidget/*`）是**内嵌 Chromium(CEF) 网页**。
实测该 Chromium 开着 **DevTools 远程调试端口**（本机 `127.0.0.1:8086`）：

    GET /json/version -> {"Browser":"Chrome/102.0.5005.115","User-Agent":"XtItClient", ...}

这意味着：**这些页面可以用标准 CDP 全量脚本化**（读 DOM、写 Monaco 编辑器、
调页面暴露的桥接函数），无需鼠标坐标自动化 —— 也就避开了此前用 Win32 窗口 API
把 Qt 主窗口玩黑屏的那类风险。

能做什么 / 不能做什么（都已实测钉死）
------------------------------------
* 能：`Target.getTargets`、`Runtime.evaluate`（在任意已打开页面里跑 JS）。
* 不能：`/json/new` -> "Could not create new page"；CDP `Target.createTarget`
  -> `{"code":-32000,"message":"Not supported"}`。**我们无法自己开一个页面**，
  只能驱动客户端已经打开的面板。
* 因此 `watch` 子命令是核心：等客户端把某个面板开起来，再对它下手。

策略编辑器页暴露的桥接面（从 `aistrategyedit.zip` 的 JS 里逆向得到）
------------------------------------------------------------------
页面用**双桥**与原生通信，二者取其一：

    window.CefViewQuery({request: JSON.stringify({label: <名>, value: {...}})})
    window.QMTQueryBridge.invokeMethod(<名>, JSON.stringify({type:<名>, value:{...}}))

已知消息名：`初始化`、`文件内容`、`修改代码`、`读取编辑器内容`、`状态`、
`内置Python`、`原生Python`、`网络策略`、`用户`、`已用`、`剩余`、`余量`、
`单股趋势策略`、`多因子策略`。
宿主（客户端）还会把 `getFileContent()` / `setRequestEnabled()` 绑进页面
（经 `__resolveGetFileContent__` 等 hook 注入）。

⚠️ 边界（不要误解）：**登记进注册树的那一次「保存/编译」仍是原生动作**，
页面本身没有"保存到策略列表"的桥接。本工具能自动化的是**编辑器内容**
（把 52 KB 源码一次性灌进去、再读回来核对），**不能**替客户端点那个按钮。
别把"能写编辑器"当成"能注册策略"。

用法（需用托管 venv，它带 `websockets`）
--------------------------------------
    python scripts/qmt_cef_cdp.py find                 # 找调试端口
    python scripts/qmt_cef_cdp.py list                 # 列出已打开页面
    python scripts/qmt_cef_cdp.py watch --timeout 120  # 等页面出现并 dump 桥接面
    python scripts/qmt_cef_cdp.py inspect              # 逐页 dump 桥接/编辑器状态
    python scripts/qmt_cef_cdp.py eval --expr "document.title"
    python scripts/qmt_cef_cdp.py inject-file --file P:\\stock\\gd_qmt\\python\\qmt_work_agent.py
    python scripts/qmt_cef_cdp.py read-editor --out logs/editor_dump.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

#: 本机实测端口；CEF 默认 9222，QMT 用 8086
_DEFAULT_PORTS = (8086, 9222, 9223, 9229)

#: 页面里可能存在的桥接/宿主注入函数（inspect 用；仅探测存在性，不做假设）
_BRIDGE_GLOBALS = (
    "CefViewQuery", "QMTQueryBridge", "getFileContent", "setRequestEnabled",
    "isIframe", "monaco", "require",
    "__resolveGetFileContent__", "__resolveSetRequestEnabled__",
    "__resolveSetFileId__", "__resolveSetApiKey__",
)

_INSPECT_JS = """
(function () {
  var out = { href: location.href, title: document.title, bridge: {} };
  %(names)s.forEach(function (k) { out.bridge[k] = typeof window[k]; });
  try {
    var m = (window.monaco && monaco.editor && monaco.editor.getModels)
      ? monaco.editor.getModels() : [];
    out.models = m.map(function (x) {
      return { lang: x.getLanguageId(), len: x.getValue().length };
    });
  } catch (e) { out.modelsErr = String(e); }
  return out;
})()
""" % {"names": json.dumps(list(_BRIDGE_GLOBALS))}

_READ_JS = """
(function () {
  try {
    if (window.monaco && monaco.editor && monaco.editor.getModels) {
      var ms = monaco.editor.getModels();
      if (ms.length) { return { via: "monaco", content: ms[0].getValue() }; }
    }
  } catch (e) {}
  try {
    if (typeof window.getFileContent === "function") {
      return { via: "getFileContent", content: String(window.getFileContent() || "") };
    }
  } catch (e) {}
  return { via: null, content: "" };
})()
"""

#: 灌入编辑器：优先直接写 Monaco 模型（最可靠、不依赖客户端消息路由），
#: 退而用页面自己的双桥发 `修改代码`。
_INJECT_JS = """
(function (code) {
  var r = { viaMonaco: false, viaBridge: null, err: null };
  try {
    if (window.monaco && monaco.editor && monaco.editor.getModels) {
      var ms = monaco.editor.getModels();
      if (ms.length) { ms[0].setValue(code); r.viaMonaco = true; r.models = ms.length; }
    }
  } catch (e) { r.err = String(e); }
  if (!r.viaMonaco) {
    var payload = JSON.stringify({ type: "修改代码", value: { content: code } });
    try {
      if (window.QMTQueryBridge && window.QMTQueryBridge.invokeMethod) {
        window.QMTQueryBridge.invokeMethod("修改代码", payload); r.viaBridge = "QMTQueryBridge";
      } else if (window.CefViewQuery) {
        window.CefViewQuery({ request: JSON.stringify({ label: "修改代码",
          value: { content: code } }) });
        r.viaBridge = "CefViewQuery";
      }
    } catch (e) { r.err2 = String(e); }
  }
  return r;
})(%(code)s)
"""


# ---------------------------------------------------------------------------
def _http_json(url, timeout=4.0):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def probe_port(port, host="127.0.0.1"):
    """端口是不是 CEF 调试口？返回 version dict 或 None。"""
    try:
        with socket.create_connection((host, port), timeout=0.6):
            pass
    except OSError:
        return None
    try:
        v = _http_json("http://%s:%d/json/version" % (host, port))
    except Exception:
        return None
    if not isinstance(v, dict) or "webSocketDebuggerUrl" not in v:
        return None
    return v


def _qmt_pids():
    """QMT 客户端进程 PID（tasklist 在本机可用，GBK 输出）。"""
    import subprocess
    try:
        out = subprocess.check_output(["tasklist"], stderr=subprocess.STDOUT)
    except Exception:
        return set()
    text = out.decode("gbk", "replace")
    pids = set()
    for line in text.splitlines():
        low = line.lower()
        if any(k in low for k in ("xtitclient", "xtminiqmt", "xtquoter")):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(parts[1])
    return pids


def _ports_from_netstat():
    """★ 比硬编码端口更稳：CEF 端口会随版本变，但一定属于客户端进程。"""
    import subprocess
    pids = _qmt_pids()
    if not pids:
        return []
    try:
        out = subprocess.check_output(["netstat", "-ano"], stderr=subprocess.STDOUT)
    except Exception:
        return []
    ports = []
    for line in out.decode("gbk", "replace").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP" or "LISTEN" not in parts[3].upper():
            continue
        if parts[-1] not in pids:
            continue
        addr = parts[1]
        port = addr.rsplit(":", 1)[-1]
        if port.isdigit():
            ports.append(int(port))
    return sorted(set(ports))


def find_port(explicit=None, host="127.0.0.1"):
    if explicit:
        v = probe_port(int(explicit), host)
        return (int(explicit), v) if v else (int(explicit), None)
    for p in _DEFAULT_PORTS:
        v = probe_port(p, host)
        if v:
            return p, v
    for p in _ports_from_netstat():          # 兜底：客户端自己的监听端口逐个试
        if p in _DEFAULT_PORTS:
            continue
        v = probe_port(p, host)
        if v:
            return p, v
    return None, None


def list_targets(port, host="127.0.0.1"):
    try:
        d = _http_json("http://%s:%d/json/list" % (host, port))
        return d if isinstance(d, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
async def _eval_ws(ws_url, expression, timeout=15.0, await_promise=True):
    import websockets
    async with websockets.connect(ws_url, origin=None,
                                  max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": await_promise},
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg.get("id") == 1:
                return msg


def eval_in_target(ws_url, expression, timeout=15.0):
    return asyncio.run(_eval_ws(ws_url, expression, timeout))


async def _browser_targets(ws_url, timeout=8.0):
    import websockets
    async with websockets.connect(ws_url, origin=None,
                                  max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg.get("id") == 1:
                return msg.get("result", {}).get("targetInfos", [])


def browser_targets(ws_url, timeout=8.0):
    return asyncio.run(_browser_targets(ws_url, timeout))


def pick_targets(targets, match=None, index=None):
    if index is not None:
        return targets[index:index + 1]
    if match:
        low = match.lower()
        sel = [t for t in targets
               if low in str(t.get("url", "")).lower()
               or low in str(t.get("title", "")).lower()]
        return sel
    return targets


def _run_eval(args, expression, label):
    port, ver = find_port(args.port)
    if not port or ver is None:
        print("[FAIL] 没找到 CEF 调试端口（QMT 在跑吗？）—— 试 `find --port N`")
        return 1
    targets = list_targets(port)
    sel = pick_targets(targets, args.match, args.index)
    if not sel:
        print("[FAIL] 没有匹配的页面（当前 %d 个）。先 `list` 看看，或 `watch` 等它出现。"
              % len(targets))
        return 2
    rc = 0
    for t in sel:
        ws_url = t.get("webSocketDebuggerUrl")
        print("--- target:", t.get("title"), "|", str(t.get("url"))[:90])
        if not ws_url:
            print("    [skip] 无 webSocketDebuggerUrl（可能已被别的会话占用）")
            continue
        try:
            msg = eval_in_target(ws_url, expression, args.timeout)
        except Exception as exc:
            print("    [FAIL] %s" % exc)
            rc = 3
            continue
        res = msg.get("result", {})
        if "exceptionDetails" in res:
            print("    [JS ERROR]", json.dumps(res["exceptionDetails"],
                                              ensure_ascii=False)[:400])
            rc = 3
            continue
        val = res.get("result", {}).get("value")
        if isinstance(val, str) and len(val) > 4000 and not args.full:
            print("    %s = (%d chars, 用 --full 或 --out 看全文)" % (label, len(val)))
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(val)
                print("    已写 %s" % args.out)
        elif args.out and isinstance(val, str):
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(val)
            print("    %s -> %s (%d chars)" % (label, args.out, len(val)))
        else:
            print("    %s = %s" % (label, json.dumps(val, ensure_ascii=False)[:1500]))
    return rc


# ---------------------------------------------------------------------------
def cmd_find(args):
    if args.port:
        v = probe_port(int(args.port))
        print("port %s -> %s" % (args.port, "CEF" if v else "非 CEF/未监听"))
        if v:
            print("  product:", v.get("Browser"), " UA:", v.get("User-Agent"))
        return 0 if v else 1
    found = []
    for p in _DEFAULT_PORTS:
        v = probe_port(p)
        if v:
            found.append((p, v))
            print("[OK] %d 是 CEF 调试口: %s | UA=%s"
                  % (p, v.get("Browser"), v.get("User-Agent")))
    if not found:
        print("[FAIL] 常见端口都没有 CEF 调试口。QMT 未运行，或该版本未开远程调试。")
        return 1
    return 0


def cmd_list(args):
    port, ver = find_port(args.port)
    if not port or ver is None:
        print("[FAIL] 无 CEF 调试口")
        return 1
    targets = list_targets(port)
    print("port=%d  targets=%d" % (port, len(targets)))
    for i, t in enumerate(targets):
        print("  [%d] %-10s %-28s %s" % (i, t.get("type"), str(t.get("title"))[:28],
                                         str(t.get("url"))[:90]))
    if not targets:
        print("  （空：客户端当前没有打开任何网页型面板）")
    return 0


def cmd_watch(args):
    """等客户端打开网页面板；出现即 dump 桥接面（本工具的核心子命令）。"""
    port, ver = find_port(args.port)
    if not port or ver is None:
        print("[FAIL] 无 CEF 调试口（QMT 在跑吗？）")
        return 1
    print("监听 %d，等网页面板出现（最多 %ds）..." % (port, args.timeout))
    deadline = time.time() + args.timeout
    seen = set()
    while time.time() < deadline:
        for t in list_targets(port):
            key = t.get("id")
            if key in seen:
                continue
            seen.add(key)
            print("\n[+] 新页面: %s" % t.get("title"))
            print("    url: %s" % t.get("url"))
            ws_url = t.get("webSocketDebuggerUrl")
            if ws_url:
                try:
                    msg = eval_in_target(ws_url, _INSPECT_JS)
                    val = msg.get("result", {}).get("result", {}).get("value")
                    print("    桥接面: %s" % json.dumps(val, ensure_ascii=False)[:900])
                except Exception as exc:
                    print("    inspect 失败: %s" % exc)
        time.sleep(1.0)
    if not seen:
        print("[!] 超时，期间没有页面出现。请在 QMT 里打开目标面板（如策略编辑器）再重跑。")
        return 2
    return 0


def cmd_inspect(args):
    return _run_eval(args, _INSPECT_JS, "inspect")


def cmd_eval(args):
    return _run_eval(args, args.expr, "eval")


def cmd_read_editor(args):
    return _run_eval(args, _READ_JS, "editor")


def cmd_inject_file(args):
    if not os.path.exists(args.file):
        print("[FAIL] 文件不存在: %s" % args.file)
        return 1
    src = open(args.file, "rb").read()
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            code = src.decode(enc)
            print("[i] 读入 %s（%s，%d 字符）" % (args.file, enc, len(code)))
            break
        except UnicodeDecodeError:
            continue
    else:
        print("[FAIL] 无法解码 %s" % args.file)
        return 1
    expr = _INJECT_JS % {"code": json.dumps(code)}
    return _run_eval(args, expr, "inject")


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="QMT 内置 Chromium(CEF) 的 CDP 客户端")
    ap.add_argument("cmd", choices=("find", "list", "watch", "inspect", "eval",
                                    "inject-file", "read-editor"))
    ap.add_argument("--port", type=int, help="CEF 调试端口（默认自动探测 8086/9222…）")
    ap.add_argument("--match", help="按 url/title 子串筛页面")
    ap.add_argument("--index", type=int, help="按序号选页面（见 list）")
    ap.add_argument("--expr", help="eval：要执行的 JS 表达式")
    ap.add_argument("--file", help="inject-file：要灌入编辑器的文件")
    ap.add_argument("--out", help="把字符串结果写到文件")
    ap.add_argument("--full", action="store_true", help="长字符串不截断")
    ap.add_argument("--timeout", type=int, default=15, help="watch 等待秒数 / eval 超时")
    args = ap.parse_args(argv)

    if args.cmd == "eval" and not args.expr:
        ap.error("eval 需要 --expr")
    if args.cmd == "inject-file" and not args.file:
        ap.error("inject-file 需要 --file")
    if args.cmd == "watch" and args.timeout == 15:
        args.timeout = 120
    args.timeout_s = args.timeout

    return {"find": cmd_find, "list": cmd_list, "watch": cmd_watch,
            "inspect": cmd_inspect, "eval": cmd_eval,
            "inject-file": cmd_inject_file,
            "read-editor": cmd_read_editor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
