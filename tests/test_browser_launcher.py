"""Chrome must not share the daemon's macOS coalition.

Regression: force-quitting "Google Chrome for Testing" from the Dock (and,
earlier, from the Cmd+Opt+Esc panel) signalled every process in its
coalition. agent-browser's Chrome was spawned inside a leashd tmux pane, so
that coalition held the daemon, tmux and every claude pane, and leashd shut
down mid-session.
"""

from __future__ import annotations

import ctypes
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from leashd import browser_launcher
from leashd.browser_launcher import (
    EXECUTABLE_PATH_ENV,
    JOB_LABEL_PREFIX,
    REAL_EXECUTABLE_ENV,
    browser_isolation_env,
    install_launcher,
    job_plist,
    launcher_path,
    launcher_script,
    resolve_browser_executable,
)

_CHROME = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


def _install_chrome(home: Path, version: str) -> Path:
    exe = home / ".agent-browser" / "browsers" / f"chrome-{version}" / _CHROME
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def _fake_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


class TestResolveBrowserExecutable:
    def test_picks_newest_install_by_version_not_name(self, tmp_path):
        _install_chrome(tmp_path, "99.0.1.2")
        newest = _install_chrome(tmp_path, "151.0.7922.34")

        assert resolve_browser_executable({}, tmp_path) == newest

    def test_none_without_an_install(self, tmp_path):
        assert resolve_browser_executable({}, tmp_path) is None

    def test_user_choice_wins(self, tmp_path):
        _install_chrome(tmp_path, "151.0.7922.34")
        chosen = _fake_executable(tmp_path / "my-chrome", "exit 0")

        resolved = resolve_browser_executable(
            {REAL_EXECUTABLE_ENV: str(chosen)}, tmp_path
        )

        assert resolved == chosen

    def test_missing_user_choice_is_not_replaced(self, tmp_path):
        _install_chrome(tmp_path, "151.0.7922.34")

        resolved = resolve_browser_executable(
            {REAL_EXECUTABLE_ENV: str(tmp_path / "gone")}, tmp_path
        )

        assert resolved is None


class TestBrowserIsolationEnv:
    def test_routes_agent_browser_through_the_launcher_on_macos(self, tmp_path):
        _install_chrome(tmp_path, "151.0.7922.34")
        environ = {"LEASHD_BROWSER_LAUNCHER_DIR": str(tmp_path / "bin")}

        env = browser_isolation_env(environ, platform="darwin", home=tmp_path)

        launcher = tmp_path / "bin" / "browser-launcher"
        assert env == {EXECUTABLE_PATH_ENV: str(launcher)}
        assert launcher.read_text() == launcher_script()
        assert os.access(launcher, os.X_OK)

    def test_off_macos_leaves_agent_browser_alone(self, tmp_path):
        _install_chrome(tmp_path, "151.0.7922.34")

        assert browser_isolation_env({}, platform="linux", home=tmp_path) == {}

    def test_no_chrome_keeps_agent_browser_discovery(self, tmp_path):
        environ = {"LEASHD_BROWSER_LAUNCHER_DIR": str(tmp_path / "bin")}

        assert browser_isolation_env(environ, platform="darwin", home=tmp_path) == {}
        assert not (tmp_path / "bin").exists()

    def test_user_executable_becomes_the_launched_browser(self, tmp_path):
        chosen = _fake_executable(tmp_path / "my-chrome", "exit 0")
        environ = {
            "LEASHD_BROWSER_LAUNCHER_DIR": str(tmp_path / "bin"),
            EXECUTABLE_PATH_ENV: str(chosen),
        }

        env = browser_isolation_env(environ, platform="darwin", home=tmp_path)

        assert env[REAL_EXECUTABLE_ENV] == str(chosen)
        assert env[EXECUTABLE_PATH_ENV] == str(tmp_path / "bin" / "browser-launcher")

    def test_reapplying_does_not_wrap_the_launcher_in_itself(self, tmp_path):
        _install_chrome(tmp_path, "151.0.7922.34")
        environ = {"LEASHD_BROWSER_LAUNCHER_DIR": str(tmp_path / "bin")}
        first = browser_isolation_env(environ, platform="darwin", home=tmp_path)

        again = browser_isolation_env(
            {**environ, **first}, platform="darwin", home=tmp_path
        )

        assert again == first
        assert REAL_EXECUTABLE_ENV not in again


class TestLauncherScript:
    def test_runs_this_interpreter_isolated(self):
        script = launcher_script()

        assert script.startswith("#!/bin/sh\n")
        assert f"{sys.executable} -I -m leashd.browser_launcher launch" in script
        assert script.rstrip().endswith('"$@"')

    def test_install_rewrites_a_stale_launcher(self, tmp_path):
        path = tmp_path / "browser-launcher"
        path.write_text("#!/bin/sh\nexec /old/python\n")

        install_launcher(path)

        assert path.read_text() == launcher_script()
        assert os.access(path, os.X_OK)

    def test_default_location_is_the_leashd_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert launcher_path({}) == tmp_path / ".leashd" / "bin" / "browser-launcher"


class TestJobPlist:
    def test_one_shot_interactive_job(self):
        job = plistlib.loads(
            job_plist("com.leashd.browser.1", ["/x", "--a"], {"HOME": "/h"}, "/cwd")
        )

        assert job["Label"] == "com.leashd.browser.1"
        assert job["ProgramArguments"] == ["/x", "--a"]
        assert job["EnvironmentVariables"] == {"HOME": "/h"}
        assert job["WorkingDirectory"] == "/cwd"
        assert job["RunAtLoad"] is True
        assert job["KeepAlive"] is False
        assert job["ProcessType"] == "Interactive"


class TestLaunchFallback:
    def test_runs_chrome_directly_when_launchd_refuses(self, monkeypatch, tmp_path):
        chrome = _fake_executable(tmp_path / "chrome", "exit 0")
        monkeypatch.setenv(REAL_EXECUTABLE_ENV, str(chrome))
        monkeypatch.setattr(browser_launcher.sys, "platform", "darwin")
        monkeypatch.setattr(browser_launcher, "bootstrap_job", lambda _plist: False)
        execs: list[tuple[Path, list[str]]] = []

        def fake_execv(path, argv):
            execs.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(browser_launcher.os, "execv", fake_execv)

        with pytest.raises(SystemExit):
            browser_launcher.launch(["--remote-debugging-port=0"])

        assert execs == [(chrome, [str(chrome), "--remote-debugging-port=0"])]

    def test_no_browser_exits_with_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setenv(REAL_EXECUTABLE_ENV, str(tmp_path / "gone"))

        assert browser_launcher.launch([]) == 127

    def test_unknown_subcommand_is_a_usage_error(self):
        assert browser_launcher.main(["bogus"]) == 2


_libc = ctypes.CDLL(None)


class _CoalitionInfo(ctypes.Structure):
    _fields_ = [
        ("coalition_id", ctypes.c_uint64 * 2),
        ("reserved", ctypes.c_uint64 * 3),
    ]


def _coalition(pid: int) -> int | None:
    info = _CoalitionInfo()
    size = _libc.proc_pidinfo(pid, 20, 0, ctypes.byref(info), ctypes.sizeof(info))
    return int(info.coalition_id[0]) if size > 0 else None


def _coalition_members(coalition: int) -> list[int]:
    listing = subprocess.run(
        ["/bin/ps", "-axo", "pid="], capture_output=True, text=True, check=True
    ).stdout.split()
    return [int(pid) for pid in listing if _coalition(int(pid)) == coalition]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _job_loaded(label: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("launchctl"),
    reason="coalitions and launchd are macOS-only",
)
class TestLaunchdIsolation:
    """Real launchd jobs with a stand-in browser that records its pid and args."""

    @pytest.fixture
    def browser(self, tmp_path):
        pid_file = tmp_path / "browser.pid"
        args_file = tmp_path / "browser.args"
        exe = _fake_executable(
            tmp_path / "chrome",
            f'echo "$@" > {args_file}\n'
            "echo 'DevTools listening on ws://127.0.0.1:9/devtools' >&2\n"
            f"echo $$ > {pid_file}\n"
            "exec /bin/sleep 60",
        )
        return exe, pid_file, args_file

    def _launch(self, exe: Path, tmp_path: Path, *args: str) -> subprocess.Popen:
        env = {**os.environ, REAL_EXECUTABLE_ENV: str(exe)}
        proc = subprocess.Popen(
            [sys.executable, "-I", "-m", "leashd.browser_launcher", "launch", *args],
            env=env,
            cwd=tmp_path,
            stderr=subprocess.PIPE,
        )
        self._label = f"{JOB_LABEL_PREFIX}.{proc.pid}"
        return proc

    def _browser_pid(self, pid_file: Path) -> int:
        assert _wait_for(
            lambda: pid_file.exists() and pid_file.read_text().strip() != ""
        )
        return int(pid_file.read_text())

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        self._label = None
        yield
        if self._label and _job_loaded(self._label):
            subprocess.run(
                ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{self._label}"],
                check=False,
            )

    def test_browser_runs_in_its_own_coalition(self, browser, tmp_path):
        exe, pid_file, args_file = browser
        launcher = self._launch(exe, tmp_path, "--remote-debugging-port=0")
        browser_pid = self._browser_pid(pid_file)

        assert _coalition(browser_pid) is not None
        assert _coalition(browser_pid) != _coalition(os.getpid())
        assert _coalition(launcher.pid) == _coalition(os.getpid())
        assert args_file.read_text().strip() == "--remote-debugging-port=0"

        launcher.terminate()
        _, stderr = launcher.communicate(timeout=15)

        assert b"DevTools listening on ws://127.0.0.1:9/devtools" in stderr
        assert _wait_for(lambda: not _alive(browser_pid))
        assert not _job_loaded(self._label)

    def test_force_quit_of_the_browser_spares_its_launcher(self, browser, tmp_path):
        exe, pid_file, _ = browser
        launcher = self._launch(exe, tmp_path)
        browser_pid = self._browser_pid(pid_file)
        coalition = _coalition(browser_pid)
        assert coalition is not None
        members = _coalition_members(coalition)

        assert browser_pid in members
        assert os.getpid() not in members
        assert launcher.pid not in members

        for pid in members:
            os.kill(pid, signal.SIGKILL)

        assert launcher.wait(timeout=10) != 0
        assert _alive(os.getpid())
        assert not _job_loaded(self._label)

    def test_killed_launcher_takes_the_browser_down(self, browser, tmp_path):
        exe, pid_file, _ = browser
        launcher = self._launch(exe, tmp_path)
        browser_pid = self._browser_pid(pid_file)

        launcher.kill()
        launcher.wait(timeout=5)

        assert _wait_for(lambda: not _alive(browser_pid), timeout=15)
        assert _wait_for(lambda: not _job_loaded(self._label), timeout=10)

    def test_browser_exit_status_reaches_agent_browser(self, tmp_path):
        exe = _fake_executable(tmp_path / "chrome", "exit 3")

        launcher = self._launch(exe, tmp_path)

        assert launcher.wait(timeout=15) == 3
        assert not _job_loaded(self._label)
