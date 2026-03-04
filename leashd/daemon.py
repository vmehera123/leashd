"""Daemon lifecycle management for leashd."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from leashd.exceptions import DaemonError

_LEASHD_DIR = Path.home() / ".leashd"
_PID_FILE = _LEASHD_DIR / "leashd.pid"
_DAEMON_LOG = _LEASHD_DIR / "daemon.log"


def pid_file_path() -> Path:
    """Return the PID file path."""
    return _PID_FILE


def daemon_log_path() -> Path:
    """Return the daemon log path."""
    return _DAEMON_LOG


def _read_pid() -> int | None:
    """Read PID from file. Returns None if missing or invalid."""
    try:
        text = _PID_FILE.read_text().strip()
        return int(text)
    except (FileNotFoundError, ValueError):
        return None


def _write_pid(pid: int) -> None:
    """Write PID to file atomically."""
    _LEASHD_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(pid))


def _remove_pid() -> None:
    """Remove PID file if it exists."""
    with contextlib.suppress(FileNotFoundError):
        _PID_FILE.unlink()


def cleanup() -> None:
    """Public API to remove the PID file on shutdown."""
    _remove_pid()


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it
        return True
    return True


def _find_daemon_pid() -> int | None:
    """Scan for a running `leashd _run` process when PID file is missing."""
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", "leashd _run"],  # noqa: S607
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in output.splitlines():
        with contextlib.suppress(ValueError):
            pid = int(line.strip())
            if pid != os.getpid() and _is_process_alive(pid):
                return pid
    return None


def _read_log_tail(max_bytes: int = 2000) -> str:
    """Read the tail of the daemon log for error diagnostics."""
    try:
        size = _DAEMON_LOG.stat().st_size
        with open(_DAEMON_LOG, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "(no daemon log found)"


def is_running() -> tuple[bool, int | None]:
    """Check if daemon is running. Auto-cleans stale PID files."""
    pid = _read_pid()
    if pid is None:
        # Fallback: scan for daemon process and self-heal PID file
        found_pid = _find_daemon_pid()
        if found_pid is not None:
            _write_pid(found_pid)
            return True, found_pid
        return False, None
    if _is_process_alive(pid):
        return True, pid
    # Stale PID file — process is gone
    _remove_pid()
    return False, None


def start_daemon() -> int:
    """Spawn leashd as a background process. Returns child PID.

    Raises DaemonError if already running.
    """
    running, pid = is_running()
    if running:
        raise DaemonError(f"leashd is already running (PID {pid})")

    _LEASHD_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(_DAEMON_LOG, "a")  # noqa: SIM115
    try:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "leashd", "_run"],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    finally:
        log_file.close()

    try:
        _write_pid(proc.pid)
    except Exception:
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGTERM)
        raise

    # Liveness check: give the child a moment then verify it didn't crash
    time.sleep(1.5)
    if proc.poll() is not None:
        _remove_pid()
        log_tail = _read_log_tail()
        raise DaemonError(f"leashd exited immediately after start:\n{log_tail}")

    return proc.pid


def stop_daemon() -> bool:
    """Send SIGTERM and wait up to 10s for the daemon to exit.

    Returns True if stopped cleanly. Raises DaemonError if not running.
    """
    running, pid = is_running()
    if not running or pid is None:
        raise DaemonError("leashd is not running")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid()
        return True

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            _remove_pid()
            return True
        time.sleep(0.1)

    # Timed out — process still alive
    _remove_pid()
    return False
