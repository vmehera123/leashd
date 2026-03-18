"""Tests for plugin registry."""

from __future__ import annotations

import pytest

from leashd.core.events import EventBus
from leashd.exceptions import PluginError
from leashd.plugins.base import LeashdPlugin, PluginContext, PluginMeta
from leashd.plugins.registry import PluginRegistry


class StubPlugin(LeashdPlugin):
    meta = PluginMeta(name="stub", version="1.0.0", description="Test plugin")

    def __init__(self):
        self.initialized = False
        self.started = False
        self.stopped = False

    async def initialize(self, context: PluginContext) -> None:
        self.initialized = True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def registry():
    return PluginRegistry()


@pytest.fixture
def plugin_context(config):
    return PluginContext(event_bus=EventBus(), config=config)


class TestPluginRegistry:
    def test_register_and_get(self, registry):
        plugin = StubPlugin()
        registry.register(plugin)
        assert registry.get("stub") is plugin

    def test_get_nonexistent(self, registry):
        assert registry.get("nope") is None

    def test_duplicate_registration_raises(self, registry):
        registry.register(StubPlugin())
        with pytest.raises(PluginError, match="already registered"):
            registry.register(StubPlugin())

    def test_plugins_list(self, registry):
        p1 = StubPlugin()
        registry.register(p1)
        assert registry.plugins == [p1]

    async def test_init_all(self, registry, plugin_context):
        plugin = StubPlugin()
        registry.register(plugin)
        await registry.init_all(plugin_context)
        assert plugin.initialized

    async def test_start_all(self, registry, plugin_context):
        plugin = StubPlugin()
        registry.register(plugin)
        await registry.init_all(plugin_context)
        await registry.start_all()
        assert plugin.started

    async def test_stop_all(self, registry, plugin_context):
        plugin = StubPlugin()
        registry.register(plugin)
        await registry.init_all(plugin_context)
        await registry.start_all()
        await registry.stop_all()
        assert plugin.stopped

    async def test_init_failure_raises_plugin_error(self, registry, plugin_context):
        class BadPlugin(LeashdPlugin):
            meta = PluginMeta(name="bad", version="0.0.1")

            async def initialize(self, context):
                raise RuntimeError("init failed")

        registry.register(BadPlugin())
        with pytest.raises(PluginError, match="failed to initialize"):
            await registry.init_all(plugin_context)

    async def test_stop_all_reverse_order(self, registry, plugin_context):
        stop_order = []

        class PluginA(LeashdPlugin):
            meta = PluginMeta(name="a", version="1.0.0")

            async def initialize(self, context):
                pass

            async def stop(self):
                stop_order.append("A")

        class PluginB(LeashdPlugin):
            meta = PluginMeta(name="b", version="1.0.0")

            async def initialize(self, context):
                pass

            async def stop(self):
                stop_order.append("B")

        class PluginC(LeashdPlugin):
            meta = PluginMeta(name="c", version="1.0.0")

            async def initialize(self, context):
                pass

            async def stop(self):
                stop_order.append("C")

        registry.register(PluginA())
        registry.register(PluginB())
        registry.register(PluginC())
        await registry.init_all(plugin_context)
        await registry.stop_all()
        assert stop_order == ["C", "B", "A"]

    async def test_stop_all_continues_on_error(self, registry, plugin_context):
        class FailStop(LeashdPlugin):
            meta = PluginMeta(name="fail_stop", version="1.0.0")

            async def initialize(self, context):
                pass

            async def stop(self):
                raise RuntimeError("stop failed")

        stub = StubPlugin()
        # fail_stop registered first, stops last (reverse order)
        registry.register(stub)
        registry.register(FailStop())
        await registry.init_all(plugin_context)
        await registry.stop_all()
        # FailStop stops last (reverse), StubPlugin stops first
        assert stub.stopped is True

    async def test_init_failure_skips_remaining(self, registry, plugin_context):
        class BadPlugin(LeashdPlugin):
            meta = PluginMeta(name="bad", version="0.0.1")

            async def initialize(self, context):
                raise RuntimeError("init failed")

        stub = StubPlugin()
        registry.register(BadPlugin())
        registry.register(stub)
        with pytest.raises(PluginError):
            await registry.init_all(plugin_context)
        assert stub.initialized is False

    def test_plugin_context_accessible(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        assert ctx.event_bus is bus
        assert ctx.config is config

    def test_registry_plugins_returns_fresh_list(self, registry):
        plugin = StubPlugin()
        registry.register(plugin)
        plugins = registry.plugins
        plugins.clear()
        # Original registry still has the plugin
        assert len(registry.plugins) == 1

    async def test_start_all_exception_propagates(self, registry, plugin_context):
        """Exception in start() wraps in PluginError and propagates."""

        class BadStart(LeashdPlugin):
            meta = PluginMeta(name="bad_start", version="1.0")

            async def initialize(self, context):
                pass

            async def start(self):
                raise RuntimeError("start boom")

        registry.register(BadStart())
        await registry.init_all(plugin_context)
        with pytest.raises(PluginError, match="failed to start"):
            await registry.start_all()

    async def test_correct_context_passed_to_initialize(self, registry, plugin_context):
        received_ctx = []

        class ContextCapture(LeashdPlugin):
            meta = PluginMeta(name="ctx_cap", version="1.0")

            async def initialize(self, context):
                received_ctx.append(context)

        registry.register(ContextCapture())
        await registry.init_all(plugin_context)
        assert len(received_ctx) == 1
        assert received_ctx[0].event_bus is plugin_context.event_bus
        assert received_ctx[0].config is plugin_context.config

    async def test_multiple_plugins_started(self, registry, plugin_context):
        started = []

        class PA(LeashdPlugin):
            meta = PluginMeta(name="pa", version="1.0")

            async def initialize(self, context):
                pass

            async def start(self):
                started.append("pa")

        class PB(LeashdPlugin):
            meta = PluginMeta(name="pb", version="1.0")

            async def initialize(self, context):
                pass

            async def start(self):
                started.append("pb")

        registry.register(PA())
        registry.register(PB())
        await registry.init_all(plugin_context)
        await registry.start_all()
        assert started == ["pa", "pb"]
