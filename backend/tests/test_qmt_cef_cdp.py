# -*- coding: utf-8 -*-
"""`scripts/qmt_cef_cdp.py` 的纯逻辑契约。

为什么值得测
------------
这个工具是**唯一**能脚本化 QMT 网页型面板的入口（CEF 远程调试，见 DEPLOY.md §2.2）。
它大部分逻辑要连活的客户端，没法在 CI 里跑；但有三块**纯函数**一旦写错就会
静默给出错误结论，必须锁住：

1. `probe_port` —— 判「这个端口是不是 CEF 调试口」。写成"能连上就算"会把
   QMT 的行情口(58600)/行情推送口(51314)误判成调试口，进而对着错误的端口发
   CDP 请求。必须是**真的拿到 `/json/version` 且含 webSocketDebuggerUrl**。
2. `pick_targets` —— 选页面。`--index`/`--match` 选错 → 把 52KB 源码灌进
   错误的页面（比如灌进帮助页），而且还"成功"了。
3. `_INSPECT_JS` / `_INJECT_JS` 的渲染 —— 用 `%` 插值拼 JS 源码，一旦插值
   出错（比如代码里含 `%` 或引号）会生成语法错误的 JS，表现是页面里静默不生效。
   `_INJECT_JS` 必须把代码 **JSON 转义**后嵌入，绝不能裸拼。
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qmt_cef_cdp as cdp  # noqa: E402


def _closed_port() -> int:
    """拿一个刚释放的本地端口（必然没人监听）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 1. probe_port：只有真 CEF 才认
# ---------------------------------------------------------------------------
def test_probe_port_returns_none_for_closed_port():
    assert cdp.probe_port(_closed_port()) is None


def test_probe_port_returns_none_for_non_cef_http():
    """能连上但返回非 CEF JSON（如普通 HTTP 服务）必须判 None，不能只看端口通。"""
    import http.server
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"hello":"world"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # 静音
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert cdp.probe_port(srv.server_address[1]) is None
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# 2. pick_targets：选择语义
# ---------------------------------------------------------------------------
_TG = [
    {"id": "a", "title": "帮助", "url": "http://x/innerApi/help.html"},
    {"id": "b", "title": "AI写策略", "url": "http://x/webstrategyedit/index.html"},
    {"id": "c", "title": "行情", "url": "http://x/webwidget/netspeed/index.html"},
]


def test_pick_targets_default_returns_all():
    assert cdp.pick_targets(_TG, None, None) == _TG


def test_pick_targets_match_is_case_insensitive_on_url_and_title():
    assert [t["id"] for t in cdp.pick_targets(_TG, "WEBSTRATEGYEDIT", None)] == ["b"]
    assert [t["id"] for t in cdp.pick_targets(_TG, "ai写策略", None)] == ["b"]
    assert cdp.pick_targets(_TG, "不存在的页面", None) == []


def test_pick_targets_index_selects_exactly_one():
    assert [t["id"] for t in cdp.pick_targets(_TG, None, 2)] == ["c"]
    assert cdp.pick_targets(_TG, None, 99) == []


# ---------------------------------------------------------------------------
# 3. 注入 JS 的渲染
# ---------------------------------------------------------------------------
def test_inspect_js_probes_every_bridge_global():
    for name in cdp._BRIDGE_GLOBALS:
        assert name in cdp._INSPECT_JS, name
    assert cdp._INSPECT_JS.count("%(") == 0, "插值应已全部完成"


def test_inject_js_json_escapes_code_and_survives_hostile_input():
    """代码里含引号/换行/%% 时，渲染结果仍必须是合法 JS 且原样带回。"""
    hostile = 'x = "q\\"uote"\ny = 100 + 1\nprint("%s" % "d")'
    rendered = cdp._INJECT_JS % {"code": json.dumps(hostile)}
    assert hostile not in rendered                  # 不能裸拼
    assert json.dumps(hostile) in rendered          # 必须 JSON 转义后嵌入
    assert rendered.count("%(") == 0
    # 取 IIFE 的实参（最后一个 `})(` 之后、末尾 `)` 之前），反解验证往返无损
    lit = rendered[rendered.rindex("})(") + 3:].strip()
    assert lit.endswith(")")
    assert json.loads(lit[:-1]) == hostile
