"""打包态后端 EXE 的端到端验收 —— 覆盖「不需要装 QMT 也能验」的那几条。

为什么单独做这一层：`verify_packaged_bridge.py` 只做**静态**核对（闭包/静态资源在不在包里），
而「连接列表会不会堆重复」「冷热分层与同步状态是否真的对外可见」必须**打真接口**才算数。
这几条**都不依赖券商客户端**：

- `POST /brokers` 幂等：`account_id` 非空时 `_resolve_account` 不校验路径，
  且路由无鉴权 ⇒ 可以凭空造一份配置连打两次，断言第二次 `reused=True`。
  ⚠️ 必须传 `autoconnect: false`：默认 `True` 时命中既有连接会去 `activate()`（真连券商），
     在没装 QMT 的机器上会失败，把「幂等」测成「连不上」。
- 冷热分层 / 每日同步：`GET /market/kline/sync-status` 与 `GET /market/kline/cache`
  是新接的观测面，字段缺失即说明打包进去的后端不是这一版。

用法：
    backend/runtimes/cp311/python.exe output/verify_packaged_backend_api.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "backend" / "dist" / "qmt_work" / "qmt_work.exe"
OUT = ROOT / "output" / "packaged_api_verify"
# 凭空造的配置：故意指向不存在的路径 —— 幂等判定只看 (broker_id, client_path, account_id)
FAKE_CLIENT_PATH = r"P:\__nonexistent_qmt_client__"
FAKE_ACCOUNT = "8888888"
BROKER_ID = "guojin"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 本机有代理时 urllib 会把 127.0.0.1 也走代理 ⇒ 必须显式禁用（等价于 curl --noproxy '*'）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, url: str, payload: dict | None = None,
         timeout: float = 20.0) -> tuple[int | None, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body[:200]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw[:200]
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))

    def render(self) -> int:
        w = max((len(n) for _, n, _ in self.rows), default=10)
        for level, name, detail in self.rows:
            print(f"  [{level}] {name.ljust(w)}  {detail}")
        bad = sum(1 for r in self.rows if r[0] == "FAIL")
        warn = sum(1 for r in self.rows if r[0] == "WARN")
        print(f"\n  合计 {len(self.rows)} 项：{len(self.rows)-bad-warn} PASS / {warn} WARN / {bad} FAIL")
        return 1 if bad else 0


def main() -> int:
    if not EXE.is_file():
        print(f"后端 EXE 不存在：{EXE}\n先跑 bash build_all.sh")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    # ⚠️ 每次运行必须是**全新的 DB**：否则上一轮落的 broker_connections 还在，
    #    第一次 POST 就已经是 `reused=True` ⇒ 「第一次必须 False」这条断言恒假红。
    run_dir = OUT / f"run-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}/api/v1"

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update({
        "QMT_CLIENT_TEST_MODE": "1",
        "QMT_CLIENT_SAFE_MODE": "1",
        "QMT_PORT": str(port),
        "QMT_PORT_FILE": str(run_dir / "port.txt"),
        "QMT_DB_PATH": str(run_dir / "app.db"),
        "QMT_LOG_DIR": str(run_dir),
        # 绝不能连用户真券商（与 conftest 同一纪律）
        "QMT_BROKER_AUTO_CONNECT": "false",
    })
    log = open(OUT / "backend.log", "wb")  # noqa: SIM115
    print(f"=== 打包态后端 API 验收 (port={port}) ===")
    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env, stdout=log,
                            stderr=subprocess.STDOUT, creationflags=0x00000200)
    rep = Report()
    try:
        t0 = time.time()
        ready = False
        while time.time() - t0 < 120:
            if proc.poll() is not None:
                rep.add("FAIL", "后端进程存活", f"提前退出 rc={proc.returncode}（见 {OUT/'backend.log'}）")
                return rep.render()
            code, _ = call("GET", f"{base}/health", timeout=3.0)
            if code == 200:
                ready = True
                break
            time.sleep(1.0)
        if not ready:
            rep.add("FAIL", "后端就绪", "120s 内 /health 未返回 200")
            return rep.render()
        rep.add("PASS", "后端就绪", f"{time.time()-t0:.1f}s 内 /health=200")

        # ── 问题 2：POST /brokers 幂等 ─────────────────────────────
        body = {"broker_id": BROKER_ID, "client_path": FAKE_CLIENT_PATH,
                "account_id": FAKE_ACCOUNT, "account_type": "STOCK",
                "autoconnect": False, "name": "幂等验收"}
        c1, r1 = call("POST", f"{base}/brokers", body)
        c2, r2 = call("POST", f"{base}/brokers", body)
        # ⚠️ 所有路由都经 `_common.ok()` 包装 ⇒ 业务字段在 `data` 里。
        #    直接在顶层取 `reused` / `conn_id` 会拿到 None，把「产品正确」测成「断言失败」
        #    （本脚本第一版就踩了，实测 2 FAIL 全是这个原因）。
        r1 = r1.get("data") if isinstance(r1, dict) else r1
        r2 = r2.get("data") if isinstance(r2, dict) else r2
        if not (isinstance(r1, dict) and isinstance(r2, dict)):
            rep.add("FAIL", "POST /brokers 幂等", f"响应非 JSON：{c1}/{r1} {c2}/{r2}")
        else:
            reused1, reused2 = r1.get("reused"), r2.get("reused")
            same = r1.get("conn_id") == r2.get("conn_id")
            rep.add("PASS" if reused1 is False else "FAIL", "第 1 次 reused=False",
                    f"conn_id={r1.get('conn_id')!r} reused={reused1!r}")
            rep.add("PASS" if reused2 is True else "FAIL", "第 2 次 reused=True",
                    f"conn_id={r2.get('conn_id')!r} reused={reused2!r}")
            rep.add("PASS" if same else "FAIL", "两次 conn_id 相同", f"{r1.get('conn_id')!r}")
            c3, r3 = call("GET", f"{base}/brokers")
            # ⚠️ 形状是 {"code":0,"message":"ok","data":[…]} —— data **就是列表**
            #    （`ok(await status_list())`），不是 {"items": [...]}。写成
            #    `data.get("items")` 会 AttributeError，把「产品没问题」测成「断言炸了」。
            items = (r3 or {}).get("data") if isinstance(r3, dict) else None
            if isinstance(items, dict):          # 兼容将来改成 {items:[…]}
                items = items.get("items")
            if isinstance(items, list):
                hit = [x for x in items
                       if x.get("account_id") == FAKE_ACCOUNT
                       and (x.get("broker_id") or "") == BROKER_ID]
                rep.add("PASS" if len(hit) == 1 else "FAIL",
                        "列表中该身份只有 1 条", f"命中 {len(hit)} 条 / 共 {len(items)} 条")
            else:
                rep.add("WARN", "列表中该身份只有 1 条", f"GET /brokers 形状意外：{str(r3)[:120]}")

        # ── 问题 7：冷热分层 + 每日同步的观测面 ─────────────────────
        cs, sr = call("GET", f"{base}/market/kline/sync-status")
        if cs == 200 and isinstance(sr, dict):
            d = sr.get("data", sr)
            keys = set(d.keys())
            rep.add("PASS" if {"enabled", "sync_time", "hot"} <= keys else "FAIL",
                    "sync-status 含 enabled/sync_time/hot", f"keys={sorted(keys)}")
            hot = d.get("hot") or {}
            rep.add("PASS" if {"hot_days", "hot_cutoff", "cold_enabled"} <= set(hot) else "FAIL",
                    "hot 块含 hot_days/hot_cutoff/cold_enabled",
                    f"hot_days={hot.get('hot_days')} cutoff={hot.get('hot_cutoff')} "
                    f"cold_enabled={hot.get('cold_enabled')} cold_path={hot.get('cold_path')}")
        else:
            rep.add("FAIL", "GET /market/kline/sync-status", f"status={cs} body={str(sr)[:120]}")

        cc, cr = call("GET", f"{base}/market/kline/cache")
        if cc == 200 and isinstance(cr, dict):
            d = cr.get("data", cr)
            need = {"rows", "hot_rows", "archive_rows", "hot_days", "hot_cutoff",
                    "cold_enabled", "hit_rate"}
            rep.add("PASS" if need <= set(d.keys()) else "FAIL",
                    "kline/cache 含冷热与命中率字段",
                    f"hot_rows={d.get('hot_rows')} archive_rows={d.get('archive_rows')} "
                    f"hit_rate={d.get('hit_rate')!r}")
        else:
            rep.add("FAIL", "GET /market/kline/cache", f"status={cc} body={str(cr)[:120]}")

        # ── 问题 1 的运行时自证面（桥接运行时是否被识别）─────────────
        cd_, dr = call("GET", f"{base}/brokers/diagnostics")
        if cd_ == 200 and isinstance(dr, dict):
            d = dr.get("data", dr)
            bundled = d.get("bundled_runtimes")
            rep.add("PASS" if bundled else "WARN", "随包桥接运行时可见",
                    f"bundled_runtimes={bundled}")
            rep.add("PASS", "diagnostics 返回连接段",
                    f"connections={len(d.get('connections') or [])}")
        else:
            rep.add("FAIL", "GET /brokers/diagnostics", f"status={cd_} body={str(dr)[:120]}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        log.close()
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
