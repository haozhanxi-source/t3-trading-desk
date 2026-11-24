"""Thread-safe TTL cache with per-key single-flight and ``refresh`` bypass."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

TTL_MARKET_SECONDS = 60.0
TTL_RESEARCH_SECONDS = 180.0
TTL_SENTINEL_SECONDS = 120.0


class TTLCache:
    """In-memory cache. Concurrent callers for the same key share one ``factory`` call.

    Exceptions raised by ``factory`` are never cached. ``refresh=True`` always recomputes
    (and stores) but still serialises with other in-flight computations of the same key.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_entries: int = 256) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._key_locks: dict[Hashable, threading.Lock] = {}
        self._global_epoch = 0
        self._epochs: dict[Hashable, int] = {}

    @staticmethod
    def _namespace_for(key: Hashable, namespace: Hashable | None = None) -> Hashable | None:
        if namespace is not None:
            return namespace
        if isinstance(key, tuple) and key:
            return key[0]
        return None

    def _epoch_unlocked(self, namespace: Hashable | None) -> int:
        if namespace is None:
            return self._global_epoch
        return self._global_epoch + self._epochs.get(namespace, 0)

    def get_epoch(self, namespace: Hashable) -> int:
        with self._lock:
            return self._epoch_unlocked(namespace)

    def bump_epoch(self, namespace: Hashable) -> int:
        with self._lock:
            self._epochs[namespace] = self._epochs.get(namespace, 0) + 1
            return self._epoch_unlocked(namespace)

    def _fresh(self, key: Hashable) -> tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._data.pop(key, None)
            return False, None
        return True, value

    def get(self, key: Hashable) -> tuple[bool, Any]:
        with self._lock:
            return self._fresh(key)

    def set(
        self,
        key: Hashable,
        value: Any,
        ttl: float,
        *,
        expected_epoch: int | None = None,
        namespace: Hashable | None = None,
    ) -> bool:
        ns = self._namespace_for(key, namespace)
        with self._lock:
            if expected_epoch is not None and self._epoch_unlocked(ns) != expected_epoch:
                return False
            self._data[key] = (self._clock() + ttl, value)
            if len(self._data) > self._max_entries:
                now = self._clock()
                for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
                    self._data.pop(k, None)
                while len(self._data) > self._max_entries:
                    self._data.pop(min(self._data, key=lambda k: self._data[k][0]), None)
            return True

    def get_or_compute(
        self,
        key: Hashable,
        ttl: float,
        factory: Callable[[], Any],
        refresh: bool = False,
        *,
        namespace: Hashable | None = None,
    ) -> tuple[Any, bool]:
        """Return ``(value, cache_hit)`` without caching stale pre-invalidation values."""
        if not refresh:
            with self._lock:
                hit, value = self._fresh(key)
            if hit:
                return value, True

        ns = self._namespace_for(key, namespace)
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                if not refresh:
                    # Another thread may have populated the entry while we waited.
                    hit, value = self._fresh(key)
                    if hit:
                        return value, True
                captured_epoch = self._epoch_unlocked(ns)
            value = factory()
            self.set(key, value, ttl, expected_epoch=captured_epoch, namespace=ns)
            return value, False

    get_or_set = get_or_compute

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._data.pop(key, None)

    def invalidate_prefix(self, prefix: Any) -> int:
        """Atomically bump namespace epoch and drop every cached entry whose tuple key starts with ``prefix``."""
        with self._lock:
            self._epochs[prefix] = self._epochs.get(prefix, 0) + 1
            to_drop = [k for k in self._data if isinstance(k, tuple) and k and k[0] == prefix]
            for k in to_drop:
                self._data.pop(k, None)
            return self._epoch_unlocked(prefix)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._key_locks.clear()
            self._global_epoch += 1


CACHE = TTLCache()
