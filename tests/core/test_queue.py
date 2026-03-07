"""Tests for KeyedAsyncQueue."""

import asyncio

import pytest

from leashd.core.queue import KeyedAsyncQueue


class TestKeyedAsyncQueue:
    async def test_sequential_within_same_key(self):
        queue = KeyedAsyncQueue()
        order: list[int] = []

        async def task(n: int) -> None:
            order.append(n)
            await asyncio.sleep(0.01)

        # Enqueue two tasks for the same key concurrently
        await asyncio.gather(
            queue.enqueue("k1", lambda: task(1)),
            queue.enqueue("k1", lambda: task(2)),
        )
        # Both should complete, and order should be 1 then 2 (FIFO)
        assert order == [1, 2]

    async def test_concurrent_across_different_keys(self):
        queue = KeyedAsyncQueue()
        started: list[str] = []
        finished: list[str] = []

        async def task(key: str) -> str:
            started.append(key)
            await asyncio.sleep(0.02)
            finished.append(key)
            return key

        # Different keys should run concurrently
        results = await asyncio.gather(
            queue.enqueue("k1", lambda: task("k1")),
            queue.enqueue("k2", lambda: task("k2")),
        )
        assert set(results) == {"k1", "k2"}
        # Both should have started before either finished
        assert len(started) == 2
        assert len(finished) == 2

    async def test_return_value(self):
        queue = KeyedAsyncQueue()

        async def compute() -> int:
            return 42

        result = await queue.enqueue("k1", compute)
        assert result == 42

    async def test_exception_propagation(self):
        queue = KeyedAsyncQueue()

        async def failing() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await queue.enqueue("k1", failing)

    async def test_is_busy(self):
        queue = KeyedAsyncQueue()
        assert not queue.is_busy("k1")

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(queue.enqueue("k1", blocking))
        await started.wait()
        assert queue.is_busy("k1")
        assert not queue.is_busy("k2")

        release.set()
        await task
        assert not queue.is_busy("k1")

    async def test_active_keys(self):
        queue = KeyedAsyncQueue()
        assert queue.active_keys() == set()

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking() -> None:
            started.set()
            await release.wait()

        task = asyncio.create_task(queue.enqueue("k1", blocking))
        await started.wait()
        assert queue.active_keys() == {"k1"}

        release.set()
        await task
        assert queue.active_keys() == set()

    async def test_error_does_not_block_subsequent_tasks(self):
        queue = KeyedAsyncQueue()

        async def failing() -> None:
            raise RuntimeError("oops")

        async def succeeding() -> str:
            return "ok"

        with pytest.raises(RuntimeError):
            await queue.enqueue("k1", failing)

        # Should still work after error
        result = await queue.enqueue("k1", succeeding)
        assert result == "ok"

    async def test_locks_pruned_after_threshold(self):
        """Locks dict is pruned when it exceeds the threshold."""
        from leashd.core.queue import _LOCK_PRUNE_THRESHOLD

        queue = KeyedAsyncQueue()

        async def noop() -> None:
            pass

        # Fill beyond the threshold
        for i in range(_LOCK_PRUNE_THRESHOLD + 10):
            await queue.enqueue(f"k{i}", noop)

        # All locks should have been pruned since none are held
        assert len(queue._locks) <= _LOCK_PRUNE_THRESHOLD

    async def test_locks_not_pruned_below_threshold(self):
        """Locks are retained when below the threshold."""
        queue = KeyedAsyncQueue()

        async def noop() -> None:
            pass

        for i in range(5):
            await queue.enqueue(f"k{i}", noop)

        # Below threshold, all locks should remain
        assert len(queue._locks) == 5
