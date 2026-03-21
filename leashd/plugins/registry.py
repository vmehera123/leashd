"""Feature registry — explicit plugin registration, no magic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.exceptions import PluginError

if TYPE_CHECKING:
    from leashd.connectors.base import BaseConnector
    from leashd.core.config import LeashdConfig
    from leashd.core.safety.audit import AuditLogger
    from leashd.plugins.base import LeashdPlugin, PluginContext
    from leashd.plugins.builtin.auto_approver import AutoApprover
    from leashd.plugins.builtin.auto_plan_reviewer import AutoPlanReviewer
    from leashd.plugins.builtin.autonomous_loop import AutonomousLoop
    from leashd.plugins.builtin.task_orchestrator import TaskOrchestrator

logger = structlog.get_logger()


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, LeashdPlugin] = {}

    def register(self, plugin: LeashdPlugin) -> None:
        name = plugin.meta.name
        if name in self._plugins:
            raise PluginError(f"Plugin already registered: {name}")
        self._plugins[name] = plugin
        logger.info("plugin_registered", name=name, version=plugin.meta.version)

    def get(self, name: str) -> LeashdPlugin | None:
        return self._plugins.get(name)

    @property
    def plugins(self) -> list[LeashdPlugin]:
        return list(self._plugins.values())

    async def init_all(self, context: PluginContext) -> None:
        for plugin in self._plugins.values():
            try:
                await plugin.initialize(context)
                logger.info("plugin_initialized", name=plugin.meta.name)
            except Exception as e:
                logger.error("plugin_init_failed", name=plugin.meta.name, error=str(e))
                raise PluginError(
                    f"Plugin {plugin.meta.name} failed to initialize: {e}"
                ) from e

    async def start_all(self) -> None:
        for plugin in self._plugins.values():
            try:
                await plugin.start()
                logger.info("plugin_started", name=plugin.meta.name)
            except Exception as e:
                logger.error("plugin_start_failed", name=plugin.meta.name, error=str(e))
                raise PluginError(
                    f"Plugin {plugin.meta.name} failed to start: {e}"
                ) from e

    async def stop_all(self) -> None:
        for plugin in reversed(self._plugins.values()):
            try:
                await plugin.stop()
            except Exception:
                logger.exception("plugin_stop_failed", name=plugin.meta.name)


class BuiltinPlugins(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    registry: PluginRegistry
    auto_approver: AutoApprover | None
    auto_plan_reviewer: AutoPlanReviewer | None
    autonomous_loop: AutonomousLoop | None
    task_orchestrator: TaskOrchestrator | None


def create_builtin_plugins(
    audit: AuditLogger,
    config: LeashdConfig,
    connector: BaseConnector | None,
    session_db_path: str,
    *,
    extra_plugins: list[LeashdPlugin] | None = None,
) -> BuiltinPlugins:
    """Instantiate and register all builtin plugins in one shot."""
    from leashd.plugins.builtin.audit_plugin import AuditPlugin
    from leashd.plugins.builtin.auto_approver import AutoApprover
    from leashd.plugins.builtin.auto_plan_reviewer import AutoPlanReviewer
    from leashd.plugins.builtin.autonomous_loop import AutonomousLoop
    from leashd.plugins.builtin.browser_tools import BrowserToolsPlugin
    from leashd.plugins.builtin.merge_resolver import MergeResolverPlugin
    from leashd.plugins.builtin.task_orchestrator import TaskOrchestrator
    from leashd.plugins.builtin.test_runner import TestRunnerPlugin
    from leashd.plugins.builtin.web_agent import WebAgentPlugin
    from leashd.plugins.builtin.web_interaction_logger import WebInteractionLogger

    BuiltinPlugins.model_rebuild()

    registry = PluginRegistry()

    for plugin in [
        AuditPlugin(audit),
        BrowserToolsPlugin(),
        TestRunnerPlugin(),
        WebAgentPlugin(),
        WebInteractionLogger(),
        MergeResolverPlugin(),
    ]:
        registry.register(plugin)

    auto_approver = None
    if config.auto_approver:
        auto_approver = AutoApprover(
            audit,
            model=config.auto_approver_model,
            max_calls_per_session=config.auto_approver_max_calls,
        )
        registry.register(auto_approver)
        logger.info(
            "auto_approver_enabled",
            model=config.auto_approver_model or "(default)",
            max_calls=config.auto_approver_max_calls,
        )

    auto_plan_reviewer = None
    if config.auto_plan:
        auto_plan_reviewer = AutoPlanReviewer(audit, model=config.auto_plan_model)
        registry.register(auto_plan_reviewer)
        logger.info(
            "auto_plan_reviewer_enabled",
            model=config.auto_plan_model or "(default)",
        )

    autonomous_loop = None
    if config.autonomous_loop:
        autonomous_loop = AutonomousLoop(
            connector,
            max_retries=config.autonomous_max_retries,
            auto_pr=config.auto_pr,
            auto_pr_base_branch=config.auto_pr_base_branch,
        )
        registry.register(autonomous_loop)
        logger.info(
            "autonomous_loop_enabled",
            max_retries=config.autonomous_max_retries,
            auto_pr=config.auto_pr,
        )

    task_orchestrator = None
    if config.task_orchestrator:
        task_orchestrator = TaskOrchestrator(
            connector=connector,
            db_path=session_db_path,
            max_retries=config.task_max_retries,
            auto_pr=config.auto_pr,
            auto_pr_base_branch=config.auto_pr_base_branch,
        )
        registry.register(task_orchestrator)
        logger.info(
            "task_orchestrator_enabled",
            max_retries=config.task_max_retries,
            auto_pr=config.auto_pr,
        )

    for plugin in extra_plugins or []:
        registry.register(plugin)

    return BuiltinPlugins(
        registry=registry,
        auto_approver=auto_approver,
        auto_plan_reviewer=auto_plan_reviewer,
        autonomous_loop=autonomous_loop,
        task_orchestrator=task_orchestrator,
    )
