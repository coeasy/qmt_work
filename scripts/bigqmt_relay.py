# -*- coding: utf-8 -*-
"""大 QMT 桥接低延迟中继（M3.1）：redis 队列 ⇌ 文件桥，agent 零改动。

为什么是「中继」而不是往 QMT 内置 Python 塞第三方库：
  大 QMT 内置的是 Python 3.6 标准库环境（禁 pip），把 redis 客户端硬搬进去既
  过不了 py3.6 兼容闸、也给券商终端引入了新故障面。中继跑在**用户自己的
  Python 3.8+** 里（与后端同机即可），对外讲 redis wire，对内读写文件桥目录，
  agent 侧完全无感 —— 这正是「Transport × Dialect 正交」的红利：换通道不改端。

数据流
------
    qmt_work 后端(RedisTransport)          本进程                 大 QMT agent(文件桥)
    rpush bigqmt:req        ──────►  BLPOP → 写 req/<sid>.json
    blpop bigqmt:resp:<sid> ◄──────  读 resp/<sid>.json → rpush → 删文件
    LRANGE bigqmt:events    ◄──────  tail events.ndjson 增量 → rpush + LTRIM

事件流是 PUSH_WITH_GAP：LTRIM 只保留最近 ``--keep-events`` 条，超出即成缺口
（上层 order_watchdog 以 QUERY_ORDER 对账兜底，这也是 POLL_DIFF 家族的通用纪律）。

用法（后端配置 connector_key=qmt.big.bridge.redis、bridge_dir=redis://…/0 时）::

    python scripts/bigqmt_relay.py --bridge-dir C:/Users/<you>/qmt_work/bigqmt_bridge \
        --redis-url redis://127.0.0.1:6379/0 --namespace bigqmt

★ 依赖：pip install redis。缺依赖时启动即报错（零 mock：不静默降级成「没事件」）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _deps():
    try:
        import redis  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "bigqmt_relay 需要 redis 客户端：pip install redis（或改用 "
            "connector_key=qmt.big.bridge.file，零依赖文件桥）") from exc
    import redis
    return redis


def write_request(bridge_dir: str, payload: dict) -> None:
    """把 redis 请求写成文件桥 req/<signal_id>.json（原子：tmp+replace）。"""
    req_dir = os.path.join(bridge_dir, "req")
    os.makedirs(req_dir, exist_ok=True)
    sid = str(payload.get("signal_id") or "")
    if not sid:
        return
    target = Path(req_dir) / f"{sid}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def forward_responses(bridge_dir: str, client, namespace: str) -> int:
    """resp/*.json → rpush {ns}:resp:<sid> 并删除文件。返回转发条数。"""
    resp_dir = Path(bridge_dir) / "resp"
    if not resp_dir.is_dir():
        return 0
    n = 0
    for p in sorted(resp_dir.glob("*.json")):
        try:
            raw = p.read_text("utf-8")
            envelope = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue          # 半写：下一轮再读
        sid = str(envelope.get("signal_id") or p.stem)
        client.rpush(f"{namespace}:resp:{sid}",
                     json.dumps(envelope, ensure_ascii=False))
        try:
            p.unlink()
        except OSError:
            pass
        n += 1
    return n


class EventTailer:
    """events.ndjson 增量 tail：记录字节偏移，只转发完整新行。

    文件轮转（.1 备份）会让偏移失效 —— 检测到体积回缩即从头重读。

    ★ 「重读出来的旧行无害」这个理由**只对了一半**（2026-10-01 修正）：旧行重放确实
      不会多投递（消费端按 seq 去重），但那句注释当年把「重读」与「消费端去重」凑成
      了一个看似安全的组合，**掩盖**了真正的坑：agent 的 ``seq`` 是**进程内**计数器，
      **策略重启后从 1 重来**，而本 tailer 是 append 转发 ⇒ 队列里会出现
      「旧会话 1..42」紧跟「新会话 1..」的 **seq 回卷**。此时消费端的旧水位 42 会把
      **新会话**里 ``seq ≤ 42`` 的记录全部过滤掉 —— 事件通道事实上变死而指标全绿。
      ⇒ 消费端（``RedisTransport.stream_events``）已改为**识别回卷并归零水位**；
        本 tailer 不需改动（它本就该原样转发），但**不能再**用「旧行重放无害」
        作为不处理回卷的理由。
    """

    def __init__(self, path: str):
        self.path = path
        self.offset = 0

    def pump(self, client, namespace: str, keep: int = 5000) -> int:
        if not os.path.exists(self.path):
            return 0
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.offset = 0
        sent = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            fh.seek(self.offset)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue      # 半行：跳过（agent 单行原子写，极少发生）
                if ev.get("seq") is None:
                    continue
                client.rpush(f"{namespace}:events", json.dumps(ev, ensure_ascii=False))
                sent += 1
            self.offset = fh.tell()
        if sent:
            client.ltrim(f"{namespace}:events", -keep, -1)
        return sent


def pump_once(bridge_dir: str, client, namespace: str, tailer: EventTailer) -> int:
    """单轮：req 队列 → 文件；文件 resp → 响应队列；events → 事件队列。

    ★ 刻意用 lpop 而不是 blpop(timeout=0)：后者在 Redis 语义里是**永久阻塞**，
      会让中继主循环卡死在第一条请求上（中继的节律由 --interval 控制）。
    """
    moved = 0
    raw = client.lpop(f"{namespace}:req")
    if raw is not None:
        try:
            write_request(bridge_dir, json.loads(raw))
            moved += 1
        except json.JSONDecodeError:
            pass               # 坏请求：丢弃（与 agent 对半写文件的处置同纪律）
    moved += forward_responses(bridge_dir, client, namespace)
    moved += tailer.pump(client, namespace)
    return moved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bridge-dir", required=True,
                    help="与大 QMT 端 agent_config.json 的 bridge_dir **完全一致**")
    ap.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    ap.add_argument("--namespace", default="bigqmt")
    ap.add_argument("--keep-events", type=int, default=5000)
    ap.add_argument("--interval", type=float, default=0.02)
    args = ap.parse_args(argv)

    redis = _deps()
    if not os.path.isdir(args.bridge_dir):
        print(f"[relay] 警告: bridge_dir 不存在（{args.bridge_dir}）—— "
              "两端路径不一致是桥接排障第一名", file=sys.stderr)
    client = redis.Redis.from_url(args.redis_url, decode_responses=True,
                                  socket_connect_timeout=3.0)
    client.ping()
    tailer = EventTailer(os.path.join(args.bridge_dir, "events.ndjson"))
    print(f"[relay] {args.redis_url} ns={args.namespace} ⇄ {args.bridge_dir} "
          f"(Ctrl-C 退出)", flush=True)
    idle_since = time.time()
    while True:
        try:
            moved = pump_once(args.bridge_dir, client, args.namespace, tailer)
            if moved:
                idle_since = time.time()
            elif time.time() - idle_since > 30.0:
                print("[relay] 30s 无任何转发：检查 QMT 策略是否在运行、"
                      "bridge_dir 两端是否一致", flush=True)
                idle_since = time.time()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001  中继不许崩，报出来继续跑
            print(f"[relay] 轮次异常: {exc}", file=sys.stderr, flush=True)
        time.sleep(max(0.005, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
