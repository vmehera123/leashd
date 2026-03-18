"""Tests for the event bus."""

from __future__ import annotations

import pytest

from leashd.core.events import Event, EventBus


@pytest.fixture
def bus():
    return EventBus()


class TestEventBus:
    async def test_emit_calls_subscribed_handler(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test", handler)
        await bus.emit(Event(name="test", data={"key": "value"}))

        assert len(received) == 1
        assert received[0].data["key"] == "value"

    async def test_emit_ignores_unsubscribed_events(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("other", handler)
        await bus.emit(Event(name="test"))

        assert len(received) == 0

    async def test_multiple_handlers(self, bus):
        calls = []

        async def h1(event):
            calls.append("h1")

        async def h2(event):
            calls.append("h2")

        bus.subscribe("test", h1)
        bus.subscribe("test", h2)
        await bus.emit(Event(name="test"))

        assert calls == ["h1", "h2"]

    async def test_unsubscribe(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test", handler)
        bus.unsubscribe("test", handler)
        await bus.emit(Event(name="test"))

        assert len(received) == 0

    async def test_handler_error_does_not_break_pipeline(self, bus):
        results = []

        async def bad_handler(event):
            raise RuntimeError("boom")

        async def good_handler(event):
            results.append("ok")

        bus.subscribe("test", bad_handler)
        bus.subscribe("test", good_handler)
        await bus.emit(Event(name="test"))

        assert results == ["ok"]

    async def test_emit_no_handlers(self, bus):
        # Should not raise
        await bus.emit(Event(name="nobody_listening"))

    async def test_unsubscribe_nonexistent_handler(self, bus):
        async def handler(event):
            pass

        # Should not raise
        bus.unsubscribe("test", handler)

    def test_event_data_defaults_empty(self):
        event = Event(name="x")
        assert event.data == {}

    async def test_duplicate_handler_called_twice(self, bus):
        count = []

        async def handler(event):
            count.append(1)

        bus.subscribe("test", handler)
        bus.subscribe("test", handler)
        await bus.emit(Event(name="test"))
        assert len(count) == 2

    async def test_all_handlers_fail_no_propagation(self, bus):
        async def bad1(event):
            raise RuntimeError("fail1")

        async def bad2(event):
            raise RuntimeError("fail2")

        bus.subscribe("test", bad1)
        bus.subscribe("test", bad2)
        # Should not raise
        await bus.emit(Event(name="test"))

    async def test_handler_on_multiple_events(self, bus):
        received = []

        async def handler(event):
            received.append(event.name)

        bus.subscribe("a", handler)
        bus.subscribe("b", handler)
        await bus.emit(Event(name="a"))
        await bus.emit(Event(name="b"))
        assert received == ["a", "b"]


class TestEventBusRobustness:
    """Robustness and edge case tests."""

    async def test_unsubscribe_during_emit_is_safe(self, bus):
        """Unsubscribing a handler during emit doesn't crash."""
        calls = []

        async def h1(event):
            calls.append("h1")
            bus.unsubscribe("test", h2)

        async def h2(event):
            calls.append("h2")

        bus.subscribe("test", h1)
        bus.subscribe("test", h2)
        # h1 removes h2 during iteration — should not crash
        # h2 may or may not run depending on iteration snapshot
        await bus.emit(Event(name="test"))
        assert "h1" in calls

    async def test_100_handlers_all_called(self, bus):
        count = []

        async def handler(event):
            count.append(1)

        for _ in range(100):
            bus.subscribe("mass", handler)
        await bus.emit(Event(name="mass"))
        assert len(count) == 100

    async def test_subscribe_during_emit_is_safe(self, bus):
        """Subscribing a new handler during emit must not crash.

        Because EventBus iterates the handler list directly, a new handler
        appended during iteration may or may not fire for the current event.
        The key contract is: no crash, and the new handler is available for
        future events.
        """
        calls = []

        async def late_handler(event):
            calls.append("late")

        async def subscribing_handler(event):
            calls.append("subscribing")
            bus.subscribe("test", late_handler)

        bus.subscribe("test", subscribing_handler)
        await bus.emit(Event(name="test"))

        assert "subscribing" in calls

        calls.clear()
        await bus.emit(Event(name="test"))
        assert "late" in calls

    def test_event_timestamp_populated(self):
        from datetime import datetime

        event = Event(name="x")
        assert isinstance(event.timestamp, datetime)
