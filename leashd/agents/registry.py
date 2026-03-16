"""Agent registry — config-driven agent selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from leashd.exceptions import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from leashd.agents.base import BaseAgent
    from leashd.core.config import LeashdConfig

_REGISTRY: dict[str, Callable[[LeashdConfig], BaseAgent]] = {}
_CAPABILITIES: dict[str, dict[str, str]] = {}


def get_agent(name: str, config: LeashdConfig) -> BaseAgent:
    factory = _REGISTRY.get(name)
    if not factory:
        available = ", ".join(sorted(_REGISTRY)) or "none"
        raise ConfigError(f"Unknown agent runtime: {name!r}. Available: {available}")
    return factory(config)


def register_agent(name: str, factory: Callable[[LeashdConfig], BaseAgent]) -> None:
    _REGISTRY[name] = factory


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


def _register_builtins() -> None:
    from leashd.agents.runtimes.claude_code import ClaudeCodeAgent
    from leashd.agents.runtimes.codex import CodexAgent

    register_agent("claude-code", lambda config: ClaudeCodeAgent(config))
    register_agent("codex", lambda config: CodexAgent(config))
    _CAPABILITIES["claude-code"] = {"stability": "stable"}
    _CAPABILITIES["codex"] = {"stability": "beta"}


_register_builtins()
