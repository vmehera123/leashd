"""Agent runtime registry — config-driven runtime selection.

Only ``tmux`` ships today; new runtimes register a factory here and become
selectable through ``agent_runtime`` without touching the engine wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from leashd.exceptions import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from leashd.agents.base import BaseAgent
    from leashd.core.config import LeashdConfig

Stability = Literal["stable", "beta", "experimental"]

DEFAULT_RUNTIME = "tmux"

_REGISTRY: dict[str, Callable[[LeashdConfig], BaseAgent]] = {}
_STABILITY: dict[str, Stability] = {}


def register_agent(
    name: str,
    factory: Callable[[LeashdConfig], BaseAgent],
    *,
    stability: Stability = "experimental",
) -> None:
    _REGISTRY[name] = factory
    _STABILITY[name] = stability


def get_agent(name: str, config: LeashdConfig) -> BaseAgent:
    factory = _REGISTRY.get(name)
    if not factory:
        available = ", ".join(sorted(_REGISTRY)) or "none"
        raise ConfigError(f"Unknown agent runtime: {name!r}. Available: {available}")
    return factory(config)


def get_available_runtime_names() -> list[str]:
    return sorted(_REGISTRY)


def list_runtimes() -> list[dict[str, str]]:
    return [
        {"name": name, "stability": _STABILITY.get(name, "experimental")}
        for name in sorted(_REGISTRY)
    ]


def _create_tmux_agent(config: LeashdConfig) -> BaseAgent:
    from leashd.agents.runtimes.tmux import TmuxAgent

    return TmuxAgent(config)


register_agent(DEFAULT_RUNTIME, _create_tmux_agent, stability="stable")
