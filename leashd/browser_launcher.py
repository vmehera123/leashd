"""Launch agent-browser's Chrome in its own macOS coalition.

macOS groups every process spawned under an app into that app's coalition,
and a Force Quit (Dock menu or the Cmd+Opt+Esc panel) signals the whole
coalition. agent-browser runs inside a leashd tmux pane, so the Chrome it
spawns shares the daemon's coalition, and force-quitting "Google Chrome for
Testing" also killed the daemon, tmux and every claude pane.

agent-browser runs this module as its browser executable. ``launch`` starts the
real Chrome as a one-off launchd job, which gets a coalition of its own, and
stays in place of Chrome for agent-browser: Chrome's output is relayed back,
and the two lifetimes are tied over a Unix socket so Chrome exits when the
launcher is stopped, killed or loses its parent.

Stdlib only, since it runs on every browser launch.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

EXECUTABLE_PATH_ENV = "AGENT_BROWSER_EXECUTABLE_PATH"
REAL_EXECUTABLE_ENV = "LEASHD_BROWSER_EXECUTABLE"
LAUNCHER_DIR_ENV = "LEASHD_BROWSER_LAUNCHER_DIR"
LAUNCHER_NAME = "browser-launcher"
JOB_LABEL_PREFIX = "com.leashd.browser"

_CHROME_FOR_TESTING = Path(
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
)
_CONNECT_TIMEOUT_SECONDS = 15.0
_STOP_GRACE_SECONDS = 5.0
_JOB_EXIT_TIMEOUT_SECONDS = 10
_RELAY_CHUNK = 65536
_LAUNCHCTL = "/bin/launchctl"


class _StopSignalError(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def launcher_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(LAUNCHER_DIR_ENV)
    base = Path(override) if override else Path.home() / ".leashd" / "bin"
    return base / LAUNCHER_NAME


def launcher_script() -> str:
    return (
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} -I -m leashd.browser_launcher launch "$@"\n'
    )


def install_launcher(path: Path) -> Path:
    content = launcher_script()
    with contextlib.suppress(OSError):
        if path.read_text(encoding="utf-8") == content and os.access(path, os.X_OK):
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}")
    staging.write_text(content, encoding="utf-8")
    staging.chmod(0o755)
    staging.replace(path)
    return path


def _version_key(browser_dir: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", browser_dir.name))


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_browser_executable(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path | None:
    """The Chrome agent-browser would launch: the user's pick, else its newest install."""
    environ = os.environ if environ is None else environ
    override = environ.get(REAL_EXECUTABLE_ENV)
    if override:
        candidate = Path(override).expanduser()
        return candidate if _is_executable(candidate) else None
    browsers = (home or Path.home()) / ".agent-browser" / "browsers"
    if not browsers.is_dir():
        return None
    for browser_dir in sorted(
        browsers.glob("chrome-*"), key=_version_key, reverse=True
    ):
        candidate = browser_dir / _CHROME_FOR_TESTING
        if _is_executable(candidate):
            return candidate
    return None


def browser_isolation_env(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """Env that routes agent-browser's Chrome through the launcher.

    Empty off macOS, where coalitions don't exist, and when no Chrome is found,
    so agent-browser keeps its own discovery and install hint. A user-set
    ``AGENT_BROWSER_EXECUTABLE_PATH`` becomes the browser the launcher starts.
    """
    if (platform or sys.platform) != "darwin":
        return {}
    environ = os.environ if environ is None else environ
    launcher = launcher_path(environ)
    env: dict[str, str] = {}
    configured = environ.get(EXECUTABLE_PATH_ENV)
    if configured and Path(configured).expanduser() != launcher:
        env[REAL_EXECUTABLE_ENV] = configured
    elif environ.get(REAL_EXECUTABLE_ENV):
        env[REAL_EXECUTABLE_ENV] = environ[REAL_EXECUTABLE_ENV]
    if resolve_browser_executable({**environ, **env}, home) is None:
        return {}
    install_launcher(launcher)
    env[EXECUTABLE_PATH_ENV] = str(launcher)
    return env


def job_plist(
    label: str,
    program_arguments: Sequence[str],
    environment: Mapping[str, str],
    working_directory: str,
) -> bytes:
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": list(program_arguments),
            "EnvironmentVariables": dict(environment),
            "WorkingDirectory": working_directory,
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "ExitTimeOut": _JOB_EXIT_TIMEOUT_SECONDS,
        }
    )


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            [_LAUNCHCTL, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def bootstrap_job(plist_path: Path) -> bool:
    return _launchctl("bootstrap", _gui_domain(), str(plist_path))


def bootout_job(label: str) -> bool:
    return _launchctl("bootout", f"{_gui_domain()}/{label}")


def _exit_code(status_path: Path) -> int:
    try:
        code = int(status_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 1
    return 128 - code if code < 0 else code


def _raise_stopped(signum: int, _frame: object) -> None:
    raise _StopSignalError(signum)


def relay(conn: socket.socket, parent_pid: int) -> bool:
    """Copy Chrome's output to stderr until it exits; False if the parent died first."""
    queue = select.kqueue()
    try:
        events = [select.kevent(conn.fileno(), select.KQ_FILTER_READ, select.KQ_EV_ADD)]
        if parent_pid > 1:
            events.append(
                select.kevent(
                    parent_pid,
                    select.KQ_FILTER_PROC,
                    select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    select.KQ_NOTE_EXIT,
                )
            )
        try:
            queue.control(events, 0)
        except ProcessLookupError:
            return False
        while True:
            for event in queue.control(None, 2):
                if event.filter == select.KQ_FILTER_PROC:
                    return False
                data = conn.recv(_RELAY_CHUNK)
                if not data:
                    return True
                with contextlib.suppress(OSError):
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
    finally:
        queue.close()


def _exec_directly(browser: Path, args: Sequence[str]) -> int:
    os.execv(browser, [str(browser), *args])  # noqa: S606
    return 127


def launch(args: Sequence[str]) -> int:
    browser = resolve_browser_executable()
    if browser is None:
        print("leashd browser-launcher: no Chrome executable found", file=sys.stderr)
        return 127
    if sys.platform != "darwin":
        return _exec_directly(browser, args)

    parent_pid = os.getppid()
    label = f"{JOB_LABEL_PREFIX}.{os.getpid()}"
    workdir = Path(tempfile.mkdtemp(prefix="leashd-browser-"))
    sock_path = workdir / "s"
    status_path = workdir / "status"
    plist_path = workdir / "job.plist"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    started = False
    try:
        server.bind(str(sock_path))
        server.listen(1)
        server.settimeout(_CONNECT_TIMEOUT_SECONDS)
        plist_path.write_bytes(
            job_plist(
                label,
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "leashd.browser_launcher",
                    "supervise",
                    str(sock_path),
                    str(status_path),
                    label,
                    str(browser),
                    *args,
                ],
                os.environ,
                os.getcwd(),
            )
        )
        started = bootstrap_job(plist_path)
        if not started:
            raise OSError("launchctl bootstrap failed")
        conn, _ = server.accept()
    except OSError:
        server.close()
        if started:
            bootout_job(label)
        shutil.rmtree(workdir, ignore_errors=True)
        return _exec_directly(browser, args)

    server.close()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, _raise_stopped)
    try:
        if relay(conn, parent_pid):
            return _exit_code(status_path)
        return 1
    except _StopSignalError as stop:
        return 128 + stop.signum
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        conn.close()
        bootout_job(label)
        shutil.rmtree(workdir, ignore_errors=True)


def _stop_browser(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    killer = threading.Timer(_STOP_GRACE_SECONDS, proc.kill)
    killer.daemon = True
    killer.start()


def supervise(args: Sequence[str]) -> int:
    sock_path, status_path, label, browser, *browser_args = args
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(sock_path)
    proc = subprocess.Popen(  # noqa: S603
        [browser, *browser_args],
        stdin=subprocess.DEVNULL,
        stdout=conn.fileno(),
        stderr=conn.fileno(),
    )
    launcher_gone = threading.Event()

    def watch_launcher() -> None:
        with contextlib.suppress(OSError):
            while conn.recv(_RELAY_CHUNK):
                pass
        launcher_gone.set()
        _stop_browser(proc)

    signal.signal(signal.SIGTERM, lambda *_: _stop_browser(proc))
    threading.Thread(target=watch_launcher, daemon=True).start()
    code = proc.wait()
    with contextlib.suppress(OSError):
        Path(status_path).write_text(str(code), encoding="utf-8")
    conn.close()
    if launcher_gone.is_set():
        bootout_job(label)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "launch":
        return launch(argv[1:])
    if argv and argv[0] == "supervise" and len(argv) >= 5:
        return supervise(argv[1:])
    print(
        "usage: python -m leashd.browser_launcher launch <chrome args...>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
