"""Per-key async serialization primitive.

Adapted from OpenClaw's KeyedAsyncQueue — ensures tasks for the same key
execute one at a time in FIFO order, while tasks for different keys run
concurrently.  Used by TaskOrchestrator to prevent overlapping phase
execution within a single chat.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


_LOCK_PRUNE_THRESHOLD = 100


class KeyedAsyncQueue:
    """Per-key async lock that serializes execution within a key."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def enqueue(self, key: str, task: Callable[[], Awaitable[T]]) -> T:
        """Run *task* under the lock for *key*, blocking concurrent calls."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            result = await task()
        if not lock.locked() and len(self._locks) > _LOCK_PRUNE_THRESHOLD:
            self._locks.pop(key, None)
        return result

    def is_busy(self, key: str) -> bool:
        """Return True if a task is currently executing for *key*."""
        lock = self._locks.get(key)
        return lock is not None and lock.locked()

    def active_keys(self) -> set[str]:
        """Return the set of keys with actively executing tasks."""
        return {k for k, lock in self._locks.items() if lock.locked()}
