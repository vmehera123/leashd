"""Tests for the built-in audit plugin."""

import json

import pytest

from leashd.core.events import TOOL_DENIED, Event, EventBus
from leashd.plugins.base import PluginContext


class TestAuditPlugin:
    @pytest.mark.asyncio
    async def test_audit_plugin_logs_sandbox_violation(self, config, audit_logger):
        from leashd.plugins.builtin.audit_plugin import AuditPlugin

        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = AuditPlugin(audit_logger)
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "session_id": "s1",
                    "tool_name": "Read",
                    "reason": "Path outside allowed directories",
                    "violation_type": "sandbox",
                },
            )
        )

        content = audit_logger._path.read_text()
        entry = json.loads(content.strip())
        assert entry["event"] == "security_violation"
        assert entry["tool_name"] == "Read"
        assert entry["risk_level"] == "critical"

    @pytest.mark.asyncio
    async def test_audit_plugin_ignores_non_sandbox_denials(self, config, audit_logger):
        from leashd.plugins.builtin.audit_plugin import AuditPlugin

        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = AuditPlugin(audit_logger)
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "session_id": "s1",
                    "tool_name": "Bash",
                    "reason": "Blocked by safety policy",
                },
            )
        )

        assert not audit_logger._path.exists()

    @pytest.mark.asyncio
    async def test_audit_plugin_ignores_non_sandbox_reason(self, config, audit_logger):
        from leashd.plugins.builtin.audit_plugin import AuditPlugin

        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = AuditPlugin(audit_logger)
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "session_id": "s1",
                    "tool_name": "Bash",
                    "reason": "policy",
                },
            )
        )

        assert not audit_logger._path.exists()


class TestMissingEventDataFields:
    @pytest.mark.asyncio
    async def test_sandbox_violation_missing_reason(self, config, audit_logger):
        from leashd.plugins.builtin.audit_plugin import AuditPlugin

        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = AuditPlugin(audit_logger)
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "session_id": "s1",
                    "tool_name": "Read",
                    "violation_type": "sandbox",
                },
            )
        )

        content = audit_logger._path.read_text()
        entry = json.loads(content.strip())
        assert entry["event"] == "security_violation"
        assert entry["reason"] == ""

    @pytest.mark.asyncio
    async def test_sandbox_violation_missing_tool_name(self, config, audit_logger):
        from leashd.plugins.builtin.audit_plugin import AuditPlugin

        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = AuditPlugin(audit_logger)
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "session_id": "s1",
                    "reason": "path violation",
                    "violation_type": "sandbox",
                },
            )
        )

        content = audit_logger._path.read_text()
        entry = json.loads(content.strip())
        assert entry["tool_name"] == "unknown"

    @pytest.mark.asyncio
    async def test_sandbox_violation_missing_session_id(self, config, audit_logger):
        from leashd.plugins.builtin.audit_plugin import AuditPlugin

        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = AuditPlugin(audit_logger)
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "tool_name": "Read",
                    "reason": "path violation",
                    "violation_type": "sandbox",
                },
            )
        )

        content = audit_logger._path.read_text()
        entry = json.loads(content.strip())
        assert entry["session_id"] == "unknown"
