"""Bootstrap: wires all components together."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.registry import get_agent
from leashd.core.config import LeashdConfig, ensure_leashd_dir
from leashd.core.engine import Engine, PathConfig
from leashd.core.events import EventBus
from leashd.core.interactions import InteractionCoordinator
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.policy import PolicyEngine
from leashd.core.safety.sandbox import SandboxEnforcer
from leashd.core.session import SessionManager
from leashd.git.handler import GitCommandHandler
from leashd.git.service import GitService
from leashd.middleware.auth import AuthMiddleware
from leashd.middleware.base import MiddlewareChain
from leashd.middleware.rate_limit import RateLimitMiddleware
from leashd.plugins.builtin.audit_plugin import AuditPlugin
from leashd.plugins.builtin.auto_approver import AutoApprover
from leashd.plugins.builtin.autonomous_loop import AutonomousLoop
from leashd.plugins.builtin.browser_tools import BrowserToolsPlugin
from leashd.plugins.builtin.merge_resolver import MergeResolverPlugin
from leashd.plugins.builtin.task_orchestrator import TaskOrchestrator
from leashd.plugins.builtin.test_runner import TestRunnerPlugin
from leashd.plugins.builtin.web_agent import WebAgentPlugin
from leashd.plugins.registry import PluginRegistry
from leashd.storage.memory import MemorySessionStore
from leashd.storage.sqlite import SqliteSessionStore

if TYPE_CHECKING:
    from leashd.connectors.base import BaseConnector
    from leashd.plugins.base import LeashdPlugin
    from leashd.storage.base import MessageStore, SessionStore

logger = structlog.get_logger()


def switch_log_dir(new_dir: Path, config: LeashdConfig) -> None:
    """Move the rotating file log handler to a new directory."""
    new_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.close()
            root.removeHandler(handler)
    file_handler = logging.handlers.RotatingFileHandler(
        new_dir / "app.log",
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
        )
    )
    root.addHandler(file_handler)


def _resolve_against(path: Path, base: Path) -> Path:
    """Return *path* unchanged if absolute, otherwise resolve it against *base*."""
    return path if path.is_absolute() else base / path


def _configure_logging(config: LeashdConfig, *, log_dir: Path | None = None) -> None:
    """Set up structlog with console output and optional rotating JSON file handler."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    root_logger = logging.getLogger()
    root_logger.setLevel(config.log_level)
    root_logger.handlers.clear()

    # Console handler — colored dev-friendly output
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(),
        )
    )
    root_logger.addHandler(console_handler)

    # File handler — JSON lines for machine parsing
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
            )
        )
        root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers (httpx, telegram, etc.) — they flood
    # the log with low-value HTTP transport chatter at INFO/DEBUG.
    for noisy_logger in ("httpx", "httpcore", "hpack", "telegram", "telegram.ext"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


_DEFAULT_MCP_SERVERS: dict[str, Any] = {
    "playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@0.0.41"],
    },
}


def _load_default_mcp_servers(config: LeashdConfig, project_root: Path) -> None:
    """Merge default MCP servers into config so the agent always has browser tools."""
    defaults = dict(_DEFAULT_MCP_SERVERS)

    # Override defaults from file if present (local dev from repo root)
    mcp_path = project_root / ".mcp.json"
    if mcp_path.is_file():
        try:
            data = json.loads(mcp_path.read_text())
            file_servers = data.get("mcpServers", {})
            if file_servers:
                defaults = file_servers
        except (OSError, json.JSONDecodeError, KeyError):
            logger.warning("default_mcp_json_read_failed", path=str(mcp_path))

    # Existing config entries (env overrides) win over defaults
    config.mcp_servers = {**defaults, **config.mcp_servers}


def build_engine(
    config: LeashdConfig | None = None,
    connector: BaseConnector | None = None,
    plugins: list[LeashdPlugin] | None = None,
) -> Engine:
    if config is None:
        config = LeashdConfig()  # type: ignore[call-arg]  # pydantic-settings loads from env

    # Resolve relative paths against the first approved directory
    project_base = config.approved_directories[0]

    audit_is_pinned = config.audit_log_path.is_absolute()
    storage_is_pinned = config.storage_path.is_absolute()
    log_dir_is_pinned = config.log_dir is not None and config.log_dir.is_absolute()

    resolved_audit = _resolve_against(config.audit_log_path, project_base)
    resolved_storage = _resolve_against(config.storage_path, project_base)
    resolved_log_dir = (
        _resolve_against(config.log_dir, project_base)
        if config.log_dir is not None
        else None
    )

    ensure_leashd_dir(project_base)

    _configure_logging(config, log_dir=resolved_log_dir)

    leashd_pkg_root = Path(__file__).resolve().parent.parent
    _load_default_mcp_servers(config, leashd_pkg_root)

    # Bake headless into Playwright MCP args at startup (single source of truth)
    pw = config.mcp_servers.get("playwright")
    if isinstance(pw, dict):
        pw = dict(pw)
        args_list = list(pw.get("args", []))
        if config.browser_headless and "--headless" not in args_list:
            args_list.append("--headless")
        elif not config.browser_headless and "--headless" in args_list:
            args_list.remove("--headless")
        pw["args"] = args_list
        config.mcp_servers["playwright"] = pw

    if config.browser_backend == "agent-browser":
        config.mcp_servers.pop("playwright", None)
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        if not config.browser_headless:
            os.environ.setdefault("AGENT_BROWSER_HEADED", "1")
        else:
            os.environ.pop("AGENT_BROWSER_HEADED", None)
        if config.browser_user_data_dir:
            resolved = str(Path(config.browser_user_data_dir).expanduser())
            os.environ.setdefault("AGENT_BROWSER_PROFILE", resolved)
        logger.info("browser_backend_configured", backend="agent-browser")

    if config.workspace_config_root is None:
        config.workspace_config_root = Path.home()

    logger.info(
        "engine_building",
        storage_backend=config.storage_backend,
        has_connector=connector is not None,
        policy_count=len(config.policy_files),
        log_level=config.log_level,
        approved_directories=[str(d) for d in config.approved_directories],
    )

    # Session management store — global at ~/.leashd/, never switches with /dir
    global_leashd_dir = Path.home() / ".leashd"
    global_leashd_dir.mkdir(parents=True, exist_ok=True)
    session_db_path = global_leashd_dir / "sessions.db"

    session_store: SessionStore
    if config.storage_backend == "sqlite":
        session_store = SqliteSessionStore(session_db_path)
    else:
        session_store = MemorySessionStore()

    # Per-project message store — switches with /dir
    message_store: MessageStore | None = None
    if config.storage_backend == "sqlite":
        message_store = SqliteSessionStore(resolved_storage)

    session_manager = SessionManager(store=session_store)
    agent = get_agent(config.agent_runtime, config)
    event_bus = EventBus()

    # Safety components
    policy_paths = list(config.policy_files)
    if not policy_paths:
        policies_dir = Path(__file__).parent / "policies"
        default_policy = policies_dir / "default.yaml"
        dev_tools_policy = policies_dir / "dev-tools.yaml"
        if default_policy.exists():
            policy_paths = [default_policy]
        if dev_tools_policy.exists():
            policy_paths.append(dev_tools_policy)

    policy_engine = PolicyEngine(policy_paths) if policy_paths else None
    sandbox = SandboxEnforcer(
        [*config.approved_directories, Path.home() / ".claude" / "plans"]
    )
    audit = AuditLogger(resolved_audit)

    auto_approver = None
    if config.auto_approver:
        auto_approver = AutoApprover(
            audit,
            model=config.auto_approver_model,
            max_calls_per_session=config.auto_approver_max_calls,
        )
        logger.info(
            "auto_approver_enabled",
            model=config.auto_approver_model or "(default)",
            max_calls=config.auto_approver_max_calls,
        )

    auto_plan_reviewer = None
    if config.auto_plan:
        from leashd.plugins.builtin.auto_plan_reviewer import AutoPlanReviewer

        auto_plan_reviewer = AutoPlanReviewer(audit, model=config.auto_plan_model)
        logger.info(
            "auto_plan_reviewer_enabled",
            model=config.auto_plan_model or "(default)",
        )

    approval_coordinator = None
    interaction_coordinator = None
    if connector:
        approval_coordinator = ApprovalCoordinator(connector, config)
        interaction_coordinator = InteractionCoordinator(
            connector, config, event_bus, auto_plan_reviewer=auto_plan_reviewer
        )

    autonomous_loop = None
    if config.autonomous_loop:
        autonomous_loop = AutonomousLoop(
            connector,
            max_retries=config.autonomous_max_retries,
            auto_pr=config.auto_pr,
            auto_pr_base_branch=config.auto_pr_base_branch,
        )
        logger.info(
            "autonomous_loop_enabled",
            max_retries=config.autonomous_max_retries,
            auto_pr=config.auto_pr,
        )

    task_orchestrator = None
    if config.task_orchestrator:
        task_orchestrator = TaskOrchestrator(
            connector=connector,
            db_path=str(session_db_path),
            max_retries=config.task_max_retries,
            auto_pr=config.auto_pr,
            auto_pr_base_branch=config.auto_pr_base_branch,
        )
        logger.info(
            "task_orchestrator_enabled",
            max_retries=config.task_max_retries,
            auto_pr=config.auto_pr,
        )

    # Plugins
    registry = PluginRegistry()
    registry.register(AuditPlugin(audit))
    registry.register(BrowserToolsPlugin())
    registry.register(TestRunnerPlugin())
    registry.register(WebAgentPlugin())
    registry.register(MergeResolverPlugin())
    if auto_approver:
        registry.register(auto_approver)
    if auto_plan_reviewer:
        registry.register(auto_plan_reviewer)
    if autonomous_loop:
        registry.register(autonomous_loop)
    if task_orchestrator:
        registry.register(task_orchestrator)
    for plugin in plugins or []:
        registry.register(plugin)

    # Middleware
    middleware_chain = MiddlewareChain()
    if config.allowed_user_ids:
        middleware_chain.add(AuthMiddleware(config.allowed_user_ids))
    if config.rate_limit_rpm > 0:
        middleware_chain.add(
            RateLimitMiddleware(config.rate_limit_rpm, config.rate_limit_burst)
        )

    # Git command handler
    git_handler = None
    if connector:
        git_handler = GitCommandHandler(
            service=GitService(),
            connector=connector,
            sandbox=sandbox,
            audit=audit,
            event_bus=event_bus,
        )

    logger.info(
        "engine_built",
        has_auth=bool(config.allowed_user_ids),
        has_rate_limit=config.rate_limit_rpm > 0,
        plugin_count=len(registry.plugins),
        streaming=config.streaming_enabled,
    )

    engine = Engine(
        connector=connector,
        agent=agent,
        config=config,
        session_manager=session_manager,
        policy_engine=policy_engine,
        sandbox=sandbox,
        audit=audit,
        approval_coordinator=approval_coordinator,
        auto_approver=auto_approver,
        interaction_coordinator=interaction_coordinator,
        event_bus=event_bus,
        plugin_registry=registry,
        middleware_chain=middleware_chain,
        store=session_store,
        message_store=message_store,
        git_handler=git_handler,
        path_config=PathConfig(
            audit_path=config.audit_log_path,
            storage_path=config.storage_path,
            log_dir=config.log_dir or Path(".leashd/logs"),
            audit_pinned=audit_is_pinned,
            storage_pinned=storage_is_pinned,
            log_dir_pinned=log_dir_is_pinned,
        ),
    )

    if autonomous_loop:
        autonomous_loop.set_engine(engine)

    if task_orchestrator:
        task_orchestrator.set_engine(engine)

    return engine
