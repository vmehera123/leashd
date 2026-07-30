"""Persistent browser profile hygiene for the agent-browser backend."""

import socket
from pathlib import Path

import structlog

logger = structlog.get_logger()

_RESTORE_STATE_GLOBS = ("Session_*", "Tabs_*")

_PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")

BROWSER_LAUNCH_ARGS = ("--no-startup-window",)

_DEVTOOLS_PORT_FILE = "DevToolsActivePort"

_LIVENESS_TIMEOUT_SECONDS = 0.25


def merged_launch_args(existing: str | None) -> str:
    """Fold leashd's required Chrome launch args into an ``AGENT_BROWSER_ARGS`` value.

    Headed Chrome opens its own startup window alongside the one agent-browser
    drives, leaving a ``chrome://newtab/`` page that keeps the Google new-tab
    widgets loading. ``agent-browser tab`` is window-scoped and never reports
    that page, so nothing closes it either. ``--no-startup-window`` leaves only
    the tab agent-browser controls. Merged rather than assigned so a
    user-supplied value survives.
    """
    current = [arg for arg in (existing or "").split(",") if arg]
    return ",".join(current + [a for a in BROWSER_LAUNCH_ARGS if a not in current])


def _devtools_port(profile_dir: Path) -> int | None:
    """Read the CDP port Chrome recorded for this profile, if any."""
    try:
        header = (
            (profile_dir / _DEVTOOLS_PORT_FILE)
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        return int(header.strip())
    except (OSError, UnicodeDecodeError, IndexError, ValueError):
        return None


def _port_accepting(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(_LIVENESS_TIMEOUT_SECONDS)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def profile_in_use(profile_dir: Path) -> bool:
    """Report whether a live Chrome process currently holds this profile.

    Chrome leaves ``SingletonSocket`` behind on an unclean exit and its
    ``/var/folders`` target outlives the process, so the lock files alone report
    a long-dead browser as live — which silently disabled every prune below.
    When the profile records a DevTools port, ask the port instead: a refused
    connection means nothing is holding the profile. An occupied port or a
    missing port file falls back to the lock files, which errs toward reporting
    the profile busy and skipping the prune.
    """
    port = _devtools_port(profile_dir)
    if port is not None:
        return _port_accepting(port)
    return any((profile_dir / name).exists() for name in _PROFILE_LOCKS)


def prune_restore_state(profile_dir: Path) -> int:
    """Drop Chrome's saved window/tab restore state from a persistent profile.

    Chrome replays every window recorded under ``<profile>/Sessions`` when it
    launches. An automation run that relaunches the browser per URL leaves a
    window behind each time, so the state compounds until one launch restores
    hundreds of blank windows. Cookies and local storage live elsewhere in the
    profile, so dropping this state costs no logged-in sessions.

    Skipped while the profile is in use. Returns the number of files removed.
    """
    if not profile_dir.is_dir() or profile_in_use(profile_dir):
        return 0
    removed = 0
    for sessions_dir in profile_dir.glob("*/Sessions"):
        if not sessions_dir.is_dir():
            continue
        for pattern in _RESTORE_STATE_GLOBS:
            for path in sessions_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    continue
                removed += 1
    if removed:
        logger.info(
            "browser_profile_restore_state_pruned",
            profile=str(profile_dir),
            files=removed,
        )
    return removed
