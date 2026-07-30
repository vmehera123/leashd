"""Agent registry — config-driven agent selection."""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from leashd.exceptions import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from leashd.agents.base import BaseAgent
    from leashd.core.config import LeashdConfig

_REGISTRY: dict[str, Callable[[LeashdConfig], BaseAgent]] = {}
_CAPABILITIES: dict[str, dict[str, str]] = {}

_OPTIONAL_IMPORTS: dict[str, tuple[str, str]] = {
    "claude-code": ("claude_agent_sdk", "leashd[claude-agent-sdk]"),
}


def get_agent(name: str, config: LeashdConfig) -> BaseAgent:
    factory = _REGISTRY.get(name)
    if not factory:
        available = ", ".join(sorted(_REGISTRY)) or "none"
        raise ConfigError(f"Unknown agent runtime: {name!r}. Available: {available}")
    return factory(config)


def register_agent(name: str, factory: Callable[[LeashdConfig], BaseAgent]) -> None:
    _REGISTRY[name] = factory


def missing_runtime_dependency(name: str) -> str | None:
    """Return an install hint when a runtime's optional package is absent."""
    requirement = _OPTIONAL_IMPORTS.get(name)
    if requirement is None:
        return None
    module, extra = requirement
    if importlib.util.find_spec(module) is not None:
        return None
    return (
        f"The {name!r} runtime requires the {module.replace('_', '-')} package, "
        f"which is an optional dependency. Install it with: "
        f"pip install '{extra}' (or: uv tool install '{extra}')."
    )


def get_available_runtime_names() -> list[str]:
    """Return sorted list of registered runtime names."""
    return sorted(_REGISTRY)


def list_runtimes() -> list[dict[str, str]]:
    """Return name and stability for each registered runtime."""
    return [
        {
            "name": name,
            "stability": _CAPABILITIES.get(name, {}).get("stability", "unknown"),
        }
        for name in sorted(_REGISTRY)
    ]


def _create_claude_code_agent(config: LeashdConfig) -> BaseAgent:
    hint = missing_runtime_dependency("claude-code")
    if hint:
        raise ConfigError(hint)
    from leashd.agents.runtimes.claude_code import ClaudeCodeAgent

    return ClaudeCodeAgent(config)


def _register_builtins() -> None:
    from leashd.agents.runtimes.claude_cli import ClaudeCliAgent
    from leashd.agents.runtimes.codex import CodexAgent
    from leashd.agents.runtimes.tmux import TmuxAgent

    register_agent("claude-cli", lambda config: ClaudeCliAgent(config))
    register_agent("claude-code", _create_claude_code_agent)
    register_agent("codex", lambda config: CodexAgent(config))
    register_agent("tmux", lambda config: TmuxAgent(config))
    _CAPABILITIES["claude-cli"] = {"stability": "beta"}
    _CAPABILITIES["claude-code"] = {"stability": "stable"}
    _CAPABILITIES["codex"] = {"stability": "beta"}
    _CAPABILITIES["tmux"] = {"stability": "experimental"}


_register_builtins()
