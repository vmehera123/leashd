"""Agent registry — config-driven agent selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from leashd.exceptions import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from leashd.agents.base import BaseAgent
    from leashd.core.config import LeashdConfig

_REGISTRY: dict[str, Callable[[LeashdConfig], BaseAgent]] = {}


def get_agent(name: str, config: LeashdConfig) -> BaseAgent:
    factory = _REGISTRY.get(name)
    if not factory:
        available = ", ".join(sorted(_REGISTRY)) or "none"
        raise ConfigError(f"Unknown agent runtime: {name!r}. Available: {available}")
    return factory(config)


def register_agent(name: str, factory: Callable[[LeashdConfig], BaseAgent]) -> None:
    _REGISTRY[name] = factory


def _register_builtins() -> None:
    from leashd.agents.runtimes.claude_code import ClaudeCodeAgent
    from leashd.agents.runtimes.codex import CodexAgent

    register_agent("claude-code", lambda config: ClaudeCodeAgent(config))
    register_agent("codex", lambda config: CodexAgent(config))


_register_builtins()
