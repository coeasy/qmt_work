"""限流测试：窗口配额 + 每密钥配额 + LRU 上限 + 窗口过期。"""
import time

from gateway.rate_limit import RateLimiter


def test_allows_within_limit_then_blocks():
    rl = RateLimiter(window=60, max_requests=3)
    assert rl.allow_key("a") is True
    assert rl.allow_key("a") is True
    assert rl.allow_key("a") is True
    assert rl.allow_key("a") is False  # 第 4 次超限


def test_per_key_limit_overrides_global():
    rl = RateLimiter(window=60, max_requests=1, max_keys=10)
    for _ in range(5):
        assert rl.allow_key("b", per_key_limit=5) is True
    assert rl.allow_key("b", per_key_limit=5) is False


def test_lru_cap_bounded_memory():
    rl = RateLimiter(window=60, max_requests=1000, max_keys=5)
    for i in range(100):
        rl.allow_key(f"key-{i}")
    # 远超容量，但内存中键数被 LRU 上限钳制
    assert len(rl._hits) <= 5


def test_window_expiry_allows_again():
    rl = RateLimiter(window=1, max_requests=1, max_keys=10)
    assert rl.allow_key("c") is True
    assert rl.allow_key("c") is False
    time.sleep(1.1)
    assert rl.allow_key("c") is True  # 窗口过期后放行
