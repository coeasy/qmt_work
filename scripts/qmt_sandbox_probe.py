#!/usr/bin/env python3
"""QMT 沙箱能力探针（P0）：生成 68 项探针策略 + 解析回传结果 + 给出定档结论。

为什么必须有它
--------------
2026-10-23 起各券商对大 QMT 内置策略收紧，但**每家收紧的范围不一样**（有的只封写、
有的只拦绝对路径、有的连 sqlite 内存库都封）。用别人的结论改自己的策略，必然误判。
本工具把「听说」变成「测过」：跑一遍，得到这台机器唯一的能力清单。

七组 68 项
----------
H 运行痕迹 4 / F 文件 17 / N 网络 13 / K 服务端 KV 4 / P 子进程 2 /
I 第三方库 26 / B 原生接口 2 = 68。

★ **数据库组（D）已整体移除**。实测结论：``sqlite3``（含 ``:memory:``）、MySQL、Redis
  在新规下一律不可用，逐项探测只是白等超时（redis-py 的默认重试会让一次 ``ping()``
  卡 30 秒）。既然结论确定，就不要在探针里留这 5 项制造噪音与耗时。
  需要复测时用 ``--with-db`` 显式打开，默认关闭。

★ 网络组（N）默认**不跑**。实测证据：策略内 TCP（含 127.0.0.1 回环）/HTTP/ZMQ
  会触发终端「原生监控 → 停模」，即**策略被强制停止**并可能弹防外挂提示。
  这不是一次调用失败，是策略消失。要看网络结论，必须用**模拟账号**、只跑一次。

四种状态（照抄实测口径，别自创）
--------------------------------
PASS  可用；BLOCK 被拦截；WARN 目标不可达/路径不存在；SKIP 模块未安装或未注入。

用法::

    python scripts/qmt_sandbox_probe.py emit                 # 生成策略（gbk+CRLF）
    python scripts/qmt_sandbox_probe.py emit --network       # 生成含网络组的版本
    python scripts/qmt_sandbox_probe.py emit --with-db       # 生成含数据库组的版本
    python scripts/qmt_sandbox_probe.py parse <日志文件>      # 解析并定档
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "output")

#: 探针打印前缀。与 ``connectors.bigqmt_log_tail.PREFIX`` **不是**同一个：
#: 探针是一次性工具，用独立前缀避免被正式回传通道误收。
PROBE_PREFIX = "@@QMTW_PROBE|"

#: 生成物默认编码。QMT 内置 py3.6 对 ``gbk + CRLF`` 实测通过；``gbk + LF`` 必炸。
DEFAULT_ENCODING = "gbk"

#: 分组 → 项数。**这个表就是 68 的来源**，改检查项必须同步改这里（测试会核对）。
GROUP_SIZES = {"H": 4, "F": 17, "N": 13, "K": 4, "P": 2, "I": 26, "B": 2}

#: 数据库组（默认不生成）。实测 sqlite/MySQL/Redis 全不可用，留着只会白等超时。
DB_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("D", "sqlite_mem",
     "__import__('sqlite3').connect(':memory:').execute('select 1').fetchone()"),
    ("D", "sqlite_file_abs",
     "__import__('sqlite3').connect(os.path.join(os.getcwd(), '_probe.db'))"),
    ("D", "sqlite_file_rel", "__import__('sqlite3').connect('_probe_rel.db')"),
    ("D", "mysql_conn", "__import__('pymysql').connect(host='127.0.0.1', port=3306, "
                        "user='x', password='x', connect_timeout=TIMEOUT)"),
    ("D", "redis_conn", "__import__('redis').Redis(host='127.0.0.1', port=6379, "
                        "socket_timeout=TIMEOUT).ping()"),
)

#: 探针清单：(组, 名称, 表达式)。表达式在生成物里被 ``eval``，只用内置函数与
#: ``__import__``，避免依赖生成时的命名空间。
CHECKS: tuple[tuple[str, str, str], ...] = (
    # ── H 运行痕迹：先看这两行，判断拦截发生在 Python 层还是更底层 ──────────
    ("H", "open_src", "open.__module__"),
    ("H", "socket_src", "__import__('socket').socket.__module__"),
    ("H", "cwd", "os.getcwd()"),
    ("H", "syspath", "'|'.join(sys.path[:5])"),
    # ── F 文件（17）──────────────────────────────────────────────────────
    ("F", "listdir", "len(os.listdir(os.getcwd()))"),
    ("F", "read_abs", "open(os.path.join(os.getcwd(), 'probe_sample.txt')).read()"),
    ("F", "write_abs", "open(os.path.join(os.getcwd(), '_probe_w.txt'), 'w').write('x')"),
    ("F", "read_rel", "open('probe_sample.txt').read()"),
    ("F", "write_rel", "open('_probe_rel.txt', 'w').write('x')"),
    ("F", "append", "open('_probe_rel.txt', 'a').write('x')"),
    ("F", "write_bin", "open('_probe_bin.txt', 'wb').write(b'x')"),
    ("F", "mkdir", "os.mkdir(os.path.join(os.getcwd(), 'sub'))"),
    ("F", "rename", "os.rename('_probe_rel.txt', '_probe_ren.txt')"),
    ("F", "remove", "os.remove('_probe_rel.txt')"),
    ("F", "pathlib_read",
     "__import__('pathlib').Path(os.path.join(os.getcwd(), 'probe_sample.txt')).read_bytes()"),
    ("F", "json_load",
     "__import__('json').load(open(os.path.join(os.getcwd(), 'probe_sample.json')))"),
    ("F", "csv_read",
     "list(__import__('csv').reader(open(os.path.join(os.getcwd(), 'probe_sample.csv'))))"),
    ("F", "import_helper", "__import__('qmt_probe_helper').add(1, 2)"),
    ("F", "read_txt", "open(os.path.join(os.getcwd(), 'probe_sample.txt')).read()"),
    ("F", "read_csv", "open(os.path.join(os.getcwd(), 'probe_sample.csv')).read()"),
    ("F", "read_json", "open(os.path.join(os.getcwd(), 'probe_sample.json')).read()"),
    # ── N 网络（13）：默认不跑，见模块 docstring 的停模警告 ────────────────
    ("N", "dns", "__import__('socket').gethostbyname('www.baidu.com')"),
    ("N", "tcp_public",
     "__import__('socket').create_connection(('www.baidu.com', 443), TIMEOUT) and 'ok'"),
    ("N", "tcp_loopback",
     "__import__('socket').create_connection(('127.0.0.1', 21119), TIMEOUT) and 'ok'"),
    ("N", "udp_send",
     "__import__('socket').socket(__import__('socket').AF_INET, "
     "__import__('socket').SOCK_DGRAM).sendto(b'x', ('8.8.8.8', 53))"),
    ("N", "udp_loop",
     "__import__('socket').socket(__import__('socket').AF_INET, "
     "__import__('socket').SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 21119))"),
    ("N", "http_urllib",
     "__import__('urllib.request', fromlist=['u']).urlopen("
     "'http://www.baidu.com', timeout=TIMEOUT).getcode()"),
    ("N", "http_requests",
     "__import__('requests').get('http://www.baidu.com', timeout=TIMEOUT).status_code"),
    ("N", "ws_loop",
     "__import__('websocket').create_connection('ws://127.0.0.1:8765', timeout=TIMEOUT)"),
    ("N", "ws_public",
     "__import__('websocket').create_connection('wss://echo.websocket.org', timeout=TIMEOUT)"),
    ("N", "zmq_inproc",
     "__import__('zmq').Context().socket(__import__('zmq').PAIR)"),
    ("N", "zmq_tcp",
     "__import__('zmq').Context().socket(__import__('zmq').PAIR).connect('tcp://127.0.0.1:5555')"),
    ("N", "lan",
     "__import__('socket').create_connection(('192.168.1.1', 80), TIMEOUT) and 'ok'"),
    ("N", "ssl",
     "__import__('ssl').create_default_context().wrap_socket("
     "__import__('socket').create_connection(('www.baidu.com', 443), TIMEOUT), "
     "server_hostname='www.baidu.com') and 'ok'"),
    # ── D 数据库：默认不生成（见 DB_CHECKS 与模块 docstring）──────────────
    # ── K 服务端 KV（4）：闭环下唯一被官方支持的持久化 ────────────────────
    ("K", "save_key", "save_py_key('qmtw_probe_v1', '1')"),
    ("K", "get_key", "get_py_key('qmtw_probe_v1')"),
    ("K", "get_missing", "get_py_key('qmtw_probe_not_exist_v1')"),
    ("K", "save_oversize", "save_py_key('qmtw_probe_v1_big', 'x' * 5001)"),
    # ── P 子进程（2）────────────────────────────────────────────────────
    ("P", "subprocess",
     "__import__('subprocess').check_output(['cmd', '/c', 'echo', 'x'], timeout=TIMEOUT)"),
    ("P", "os_system", "os.system('echo x')"),
    # ── I 第三方库（26）：只测 import，PASS 不代表能用 ────────────────────
    ("I", "requests", "__import__('requests')"),
    ("I", "pandas", "__import__('pandas')"),
    ("I", "numpy", "__import__('numpy')"),
    ("I", "talib", "__import__('talib')"),
    ("I", "websocket", "__import__('websocket')"),
    ("I", "zmq", "__import__('zmq')"),
    ("I", "openpyxl", "__import__('openpyxl')"),
    ("I", "xlrd", "__import__('xlrd')"),
    ("I", "scipy", "__import__('scipy')"),
    ("I", "sklearn", "__import__('sklearn')"),
    ("I", "statsmodels", "__import__('statsmodels')"),
    ("I", "matplotlib", "__import__('matplotlib')"),
    ("I", "tushare", "__import__('tushare')"),
    ("I", "pymysql", "__import__('pymysql')"),
    ("I", "redis", "__import__('redis')"),
    ("I", "sqlalchemy", "__import__('sqlalchemy')"),
    ("I", "akshare", "__import__('akshare')"),
    ("I", "numba", "__import__('numba')"),
    ("I", "h5py", "__import__('h5py')"),
    ("I", "pyarrow", "__import__('pyarrow')"),
    ("I", "dateutil", "__import__('dateutil')"),
    ("I", "pytz", "__import__('pytz')"),
    ("I", "lxml", "__import__('lxml')"),
    ("I", "bs4", "__import__('bs4')"),
    ("I", "yaml", "__import__('yaml')"),
    ("I", "psutil", "__import__('psutil')"),
    # ── B 原生接口（2）：新方案的全部落脚点 ─────────────────────────────
    ("B", "market_data",
     "len(C.get_market_data_ex(['close'], ['000001.SZ'], period='1d', count=2))"),
    ("B", "ctx_attrs", "'accountid' in dir(C)"),
)

#: 生成物骨架。刻意用 ``%`` 拼装而不是 f-string：生成物要能在 py3.6 跑。
_TEMPLATE = '''# -*- coding: %(coding)s -*-
"""QMT 沙箱能力探针（自动生成，请勿手改）。

用法：
1. 把本文件内容粘进 QMT「内置 Python 策略」编辑器（编码选 %(coding_ui)s）；
2. 把 probe_sample.txt / .csv / .json 与 qmt_probe_helper.py 放到**同一个策略目录**；
3. 用**模拟账号**运行，只跑一次，看日志面板里 @@QMTW_PROBE| 那一行。

RUN_NETWORK = %(run_network)s  # 置 True 会测网络组：实测可能触发终端停模（策略被强制停止）
"""
import json
import os
import sys

RUN_NETWORK = %(run_network)s
TIMEOUT = 3.0
PREFIX = "%(prefix)s"

CHECKS = %(checks)s

_INJECTED = ("save_py_key", "get_py_key", "passorder", "cancel",
             "get_trade_detail_data", "get_market_data", "get_market_data_ex")


def _classify(exc):
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if name in ("ImportError", "ModuleNotFoundError"):
        return "SKIP", name + ": " + text[:160]
    if name == "NameError":
        return "SKIP", name + ": " + text[:160]
    if name == "PermissionError" or "forbidden" in low or "denied" in low \\
            or "not allowed" in low:
        return "BLOCK", name + ": " + text[:160]
    if name in ("FileNotFoundError", "ConnectionError", "ConnectionRefusedError",
                "TimeoutError", "gaierror", "timeout", "SSLError", "OSError"):
        return "WARN", name + ": " + text[:160]
    if "refused" in low or "timed out" in low or "unreachable" in low \\
            or "no such file" in low:
        return "WARN", name + ": " + text[:160]
    return "BLOCK", name + ": " + text[:160]


def _one(group, name, expr):
    if group == "N" and not RUN_NETWORK:
        return {"g": "N", "k": name, "st": "SKIP", "e": "RUN_NETWORK=False"}
    try:
        value = eval(expr, {"os": os, "sys": sys, "json": json, "TIMEOUT": TIMEOUT})
        return {"g": group, "k": name, "st": "PASS", "v": str(value)[:120]}
    except Exception as exc:
        status, detail = _classify(exc)
        return {"g": group, "k": name, "st": status, "e": detail}


def run_probe(C):
    rows = []
    for group, name, expr in CHECKS:
        rows.append(_one(group, name, expr))
    payload = {"py": sys.version, "n": len(rows), "res": rows}
    print(PREFIX + json.dumps(payload, ensure_ascii=False))
    return rows


def init(C):
    run_probe(C)


def handlebar(C):
    pass
'''


def checks_for(with_db: bool = False) -> tuple[tuple[str, str, str], ...]:
    """返回本次要生成的检查项。数据库组默认不生成（结论已确定，见模块 docstring）。"""
    return tuple(CHECKS) + tuple(DB_CHECKS) if with_db else tuple(CHECKS)


def group_sizes_for(with_db: bool = False) -> dict[str, int]:
    sizes = dict(GROUP_SIZES)
    if with_db:
        sizes["D"] = len(DB_CHECKS)
    return sizes


def _render(*, run_network: bool, encoding: str,
            checks: tuple[tuple[str, str, str], ...] | None = None) -> str:
    items = checks if checks is not None else CHECKS
    checks_src = "[\n"
    for group, name, expr in items:
        checks_src += "    (%r, %r, %r),\n" % (group, name, expr)
    checks_src += "]"
    coding_ui = "GBK" if encoding == "gbk" else "UTF-8"
    return _TEMPLATE % {
        "coding": "gbk" if encoding == "gbk" else "utf-8",
        "coding_ui": coding_ui,
        "run_network": "True" if run_network else "False",
        "prefix": PROBE_PREFIX,
        "checks": checks_src,
    }


def emit(out_path: str = "", *, run_network: bool = False,
         encoding: str = DEFAULT_ENCODING, with_db: bool = False) -> str:
    """写出探针策略，返回路径。gbk 时强制 CRLF（``gbk+LF`` 必 SyntaxError）。"""
    text = _render(run_network=run_network, encoding=encoding,
                   checks=checks_for(with_db))
    target = out_path or os.path.join(OUT_DIR, "qmt_probe_strategy.py")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    newline = "\r\n" if encoding == "gbk" else "\n"
    data = text.replace("\n", newline).encode(encoding, errors="replace")
    with open(target, "wb") as fh:
        fh.write(data)
    return target


# ---------------------------------------------------------------------------
# 解析与定档
# ---------------------------------------------------------------------------
def parse_log(path: str) -> dict:
    """从日志里取出**最后一条**探针结果。取不到抛 ``ValueError``（不返回空壳）。"""
    found = None
    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            idx = line.find(PROBE_PREFIX)
            if idx < 0:
                continue
            payload = line[idx + len(PROBE_PREFIX):].strip()
            try:
                parsed = json.loads(payload)
            except ValueError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("res"), list):
                found = parsed
    if found is None:
        raise ValueError("日志里没有找到 %s 记录" % PROBE_PREFIX)
    return found


def summarize(payload: dict) -> dict:
    """按组统计状态计数。"""
    groups: dict[str, dict[str, int]] = {}
    for row in payload.get("res") or []:
        group = str(row.get("g") or "?")
        status = str(row.get("st") or "?")
        bucket = groups.setdefault(group, {"PASS": 0, "BLOCK": 0, "WARN": 0, "SKIP": 0})
        bucket[status] = bucket.get(status, 0) + 1
    return groups


def verdict(groups: dict[str, dict[str, int]]) -> str:
    """把分组计数翻译成一句人话（对应实测文档里的「结果组合」表）。"""
    def blocks(group: str) -> int:
        return groups.get(group, {}).get("BLOCK", 0)

    def passes(group: str) -> int:
        return groups.get(group, {}).get("PASS", 0)

    f_block, n_block = blocks("F"), blocks("N")
    k_pass = passes("K")
    if not groups:
        return "无结果"
    if n_block == 0 and groups.get("N", {}).get("SKIP", 0) == GROUP_SIZES["N"]:
        net_note = "网络组未运行（RUN_NETWORK=False）"
    elif n_block > 0:
        net_note = "网络被拦 %d 项" % n_block
    else:
        net_note = "网络可用"

    if f_block == 0:
        file_note = "文件读写可用"
    elif f_block >= GROUP_SIZES["F"] - 1:
        file_note = "文件全封"
    else:
        file_note = "文件部分受限（%d/%d 项被拦）" % (f_block, GROUP_SIZES["F"])

    if f_block and not k_pass:
        tail = "；KV 也不可用 ⇒ 持久化只剩板块/柜台"
    elif f_block and k_pass:
        tail = "；KV 可用 ⇒ 可用 save_py_key 做辅助持久化"
    else:
        tail = ""
    return "%s；%s%s" % (file_note, net_note, tail)


def report(payload: dict, *, out_path: str = "", with_db: bool = False) -> dict:
    groups = summarize(payload)
    doc = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "python": payload.get("py", ""),
        "total": payload.get("n", len(payload.get("res") or [])),
        "groups": groups,
        "verdict": verdict(groups),
        "notes": [
            "数据库组（sqlite/MySQL/Redis）默认不探测：实测新规下全不可用，"
            "且 redis-py 默认重试会让单次 ping 卡 30 秒。",
        ],
        "blocked": [r["g"] + "/" + r["k"] for r in payload.get("res") or []
                    if r.get("st") == "BLOCK"],
        "warn": [r["g"] + "/" + r["k"] for r in payload.get("res") or []
                 if r.get("st") == "WARN"],
        "skip": [r["g"] + "/" + r["k"] for r in payload.get("res") or []
                 if r.get("st") == "SKIP"],
    }
    if with_db:
        doc["notes"] = [n for n in doc["notes"] if "默认不探测" not in n]
    target = out_path or os.path.join(
        OUT_DIR, "qmt_probe_%s.json" % _dt.date.today().strftime("%Y%m%d"))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with io.open(target, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2))
    doc["report_path"] = target
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QMT 沙箱能力探针")
    parser.add_argument("action", choices=["emit", "parse"], help="emit=生成策略；parse=解析日志")
    parser.add_argument("target", nargs="?", default="", help="parse 时为日志路径")
    parser.add_argument("--out", default="", help="输出路径")
    parser.add_argument("--network", action="store_true", help="emit 时把网络组置为运行（有停模风险）")
    parser.add_argument("--with-db", action="store_true",
                        help="emit 时附带数据库组（默认关闭：结论已确定，且会白等超时）")
    parser.add_argument("--encoding", default=DEFAULT_ENCODING, choices=["gbk", "utf-8"])
    args = parser.parse_args(argv)

    if args.action == "emit":
        path = emit(args.out, run_network=args.network, encoding=args.encoding,
                    with_db=args.with_db)
        sizes = group_sizes_for(args.with_db)
        print("[OK] 已生成探针策略: %s" % path)
        print("     检查项 %d 项 / %d 组（%s）" % (
            sum(sizes.values()), len(sizes),
            ", ".join("%s=%d" % (k, v) for k, v in sizes.items())))
        if not args.network:
            print("     ★ 网络组未启用：策略内 TCP/HTTP 会触发终端「停模」，需模拟账号单独跑一次")
        if not args.with_db:
            print("     ★ 数据库组已省略：sqlite/MySQL/Redis 实测全不可用，逐项探测只会白等超时")
        return 0

    if not args.target:
        parser.error("parse 需要日志文件路径")
    try:
        payload = parse_log(args.target)
    except (OSError, ValueError) as exc:
        sys.stderr.write("[FAIL] %s\n" % exc)
        return 2
    doc = report(payload, out_path=args.out, with_db=args.with_db)
    print("[OK] 定档: %s" % doc["verdict"])
    for group in sorted(doc["groups"]):
        bucket = doc["groups"][group]
        print("     %s: PASS=%d BLOCK=%d WARN=%d SKIP=%d" % (
            group, bucket.get("PASS", 0), bucket.get("BLOCK", 0),
            bucket.get("WARN", 0), bucket.get("SKIP", 0)))
    print("     报告: %s" % doc["report_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
