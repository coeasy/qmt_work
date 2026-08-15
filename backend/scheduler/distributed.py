"""可选分布式调度（P2，实验性）。

目标：在多个后端进程部署时，通过 leader 选举 / 分布式锁，
保证被调度的定时任务只在一个 worker 上执行。

设计要点：
- 纯逻辑、无 broker 交互、可完全离线单测。
- 锁后端可插拔：默认内存锁（MemoryLock，单进程/测试用）；
  可选 Redis 锁（RedisLock，依赖 ``redis`` 包，仅当可导入时使用）。
- 当配置了 redis 但 ``redis`` 不可用时，自动降级为内存锁并告警，不崩溃。
"""
import logging
import time

try:  # redis 为可选依赖，不可用时降级为内存锁
    import redis  # type: ignore
except ImportError:  # pragma: no cover - 依赖可选
    redis = None

log = logging.getLogger("scheduler.distributed")


class MemoryLock:
    """进程内锁后端，单进程 / 测试场景默认实现。

    结构：``{key: (owner, expire_at)}``，expire_at 为绝对时间戳（秒）。
    """

    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}

    def _expired(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return True
        owner, expire_at = entry
        return time.time() >= expire_at

    def acquire_lock(self, key: str, owner: str, ttl_seconds: int) -> bool:
        # 锁不存在 / 已过期 / 当前持有者续约 —— 均可获取
        if key not in self._store or self._expired(key) or self._store[key][0] == owner:
            self._store[key] = (owner, time.time() + ttl_seconds)
            return True
        return False

    def release_lock(self, key: str, owner: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        if entry[0] == owner:
            del self._store[key]
            return True
        return False

    def is_leader(self, key: str, owner: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        held_owner, expire_at = entry
        if time.time() >= expire_at:  # 已过期视为失去领导权
            return False
        return held_owner == owner

    def heartbeat(self, key: str, owner: str, ttl_seconds: int) -> bool:
        """刷新 TTL（领导者续约）。非持有者或已过期则失败。"""
        entry = self._store.get(key)
        if entry is None:
            return False
        if entry[0] == owner and time.time() < entry[1]:
            self._store[key] = (owner, time.time() + ttl_seconds)
            return True
        return False


class RedisLock:
    """基于 Redis 的分布式锁后端（SET NX + 过期）。

    仅在 ``redis`` 可导入时由 DistributedScheduler 构造。连接惰性建立，
    避免在不可达场景下于构造阶段发起网络请求。
    """

    def __init__(self, redis_url: str):
        if redis is None:  # 安全护栏：不可用时不应构造本类
            raise RuntimeError("redis 不可用，无法使用 RedisLock")
        self._redis_url = redis_url
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = redis.Redis.from_url(self._redis_url)
        return self._client

    def acquire_lock(self, key: str, owner: str, ttl_seconds: int) -> bool:
        # SET key owner NX EX ttl；仅当不存在时写入成功
        return bool(self.client.set(key, owner, nx=True, ex=ttl_seconds))

    def release_lock(self, key: str, owner: str) -> bool:
        # Lua 保证仅当持有者为自己时才删除，避免误删他人锁
        lua = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        return bool(self.client.eval(lua, 1, key, owner))

    def is_leader(self, key: str, owner: str) -> bool:
        return self.client.get(key) == owner.encode() if isinstance(owner, str) else False

    def heartbeat(self, key: str, owner: str, ttl_seconds: int) -> bool:
        # 仅当仍持有锁时刷新过期时间
        lua = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        )
        return bool(self.client.eval(lua, 1, key, owner, ttl_seconds))


class DistributedScheduler:
    """分布式调度器：提供 leader 选举 / 分布式锁与「仅领导者执行」封装。

    用法：
        ds = DistributedScheduler(backend="memory")
        if ds.acquire_lock("job:sync", my_id):
            ... 成为领导者，执行任务 ...
    """

    def __init__(self, backend: str = "memory", redis_url: str = "", ttl_seconds: int = 30):
        self.backend_name = backend
        self.redis_url = redis_url
        self.ttl_seconds = ttl_seconds

        if backend == "redis":
            if redis is None:
                # redis 不可用但配置了 redis URL —— 降级为内存锁并告警
                log.warning("redis 包不可用，分布式锁降级为内存锁（仅单进程有效）")
                self._lock = MemoryLock()
                self.backend_name = "memory"
            else:
                self._lock = RedisLock(redis_url)
        else:
            self._lock = MemoryLock()

    # ---------------- 锁原语 ----------------
    def acquire_lock(self, key: str, owner: str) -> bool:
        return self._lock.acquire_lock(key, owner, self.ttl_seconds)

    def release_lock(self, key: str, owner: str) -> bool:
        return self._lock.release_lock(key, owner)

    def is_leader(self, key: str, owner: str) -> bool:
        return self._lock.is_leader(key, owner)

    def heartbeat(self, key: str, owner: str) -> bool:
        return self._lock.heartbeat(key, owner, self.ttl_seconds)

    # ---------------- 领导者执行封装 ----------------
    def schedule_once(self, key: str, owner: str, func):
        """仅当 owner 当前为领导者时执行 func，返回其结果；
        非领导者返回 {"skipped": True}。"""
        if self.is_leader(key, owner):
            return func()
        return {"skipped": True}
