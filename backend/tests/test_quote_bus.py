"""行情共享总线测试：内存模式 + Redis 真路径（fakeredis 替代真实 Redis）。

零 mock 原则下的 Redis 覆盖：用 fakeredis 在内存中模拟 Redis 服务，
使 RedisQuoteBus 的引用计数 / pub-sub 真路径可被测试，无需起真实 Redis。
"""
import time

import fakeredis
import pytest

from gateway.quote_bus import (
    InMemoryQuoteBus,
    RedisQuoteBus,
    create_quote_bus,
)


def test_inmem_refcount_basic():
    bus = InMemoryQuoteBus()
    assert bus.add_ref("600000") == 1
    assert bus.add_ref("600000") == 2
    assert bus.ref_count("600000") == 2
    assert bus.dec_ref("600000") == 1
    assert bus.dec_ref("600000") == 0
    assert bus.ref_count("600000") == 0


def test_inmem_pubsub():
    bus = InMemoryQuoteBus()
    received = []
    bus.subscribe("600000", received.append)
    bus.publish("600000", {"price": 1})
    assert received == [{"price": 1}]
    bus.unsubscribe("600000", received.append)
    bus.publish("600000", {"price": 2})
    # 退订后不再收到
    assert received == [{"price": 1}]


def _fake_redis(monkeypatch):
    """把 redis.from_url 重定向到 fakeredis，使 RedisQuoteBus 走真路径。"""
    import redis

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: fake)
    return fake


def test_redis_refcount_via_fakeredis(monkeypatch):
    _fake_redis(monkeypatch)
    bus = RedisQuoteBus("redis://fake/0")
    assert bus.connect() is True
    assert bus.add_ref("600000") == 1
    assert bus.add_ref("600000") == 2
    assert bus.ref_count("600000") == 2
    assert bus.dec_ref("600000") == 1
    assert bus.dec_ref("600000") == 0
    assert bus.ref_count("600000") == 0


def test_redis_pubsub_via_fakeredis(monkeypatch):
    _fake_redis(monkeypatch)
    bus = RedisQuoteBus("redis://fake/0")
    assert bus.connect() is True
    received = []
    bus.subscribe("600000", received.append)
    # 等待 listener 线程完成订阅
    time.sleep(0.2)
    bus.publish("600000", {"price": 9})
    deadline = time.time() + 3
    while not received and time.time() < deadline:
        time.sleep(0.02)
    assert received and received[0]["price"] == 9


def test_factory_fallback_when_disabled():
    bus = create_quote_bus(redis_url="redis://x", enabled=False)
    assert isinstance(bus, InMemoryQuoteBus)


def test_factory_fallback_when_redis_unreachable(monkeypatch):
    import redis

    def _boom(*a, **k):
        raise redis.ConnectionError("no redis")

    monkeypatch.setattr(redis, "from_url", _boom)
    bus = create_quote_bus(redis_url="redis://x", enabled=True)
    # 连不上 Redis 应优雅回退内存，绝不抛错
    assert isinstance(bus, InMemoryQuoteBus)
