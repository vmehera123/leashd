"""Tests for leashd.daemon — daemon lifecycle management."""

import os
import signal
from unittest.mock import MagicMock, patch

import pytest

from leashd.exceptions import DaemonError


@pytest.fixture
def daemon_paths(tmp_path):
    """Redirect daemon paths to a temp directory."""
    leashd_dir = tmp_path / ".leashd"
    leashd_dir.mkdir()
    pid_file = leashd_dir / "leashd.pid"
    daemon_log = leashd_dir / "daemon.log"
    with (
        patch("leashd.daemon._LEASHD_DIR", leashd_dir),
        patch("leashd.daemon._PID_FILE", pid_file),
        patch("leashd.daemon._DAEMON_LOG", daemon_log),
    ):
        yield pid_file, daemon_log, leashd_dir


class TestPidFile:
    def test_write_and_read(self, daemon_paths):
        from leashd.daemon import _read_pid, _write_pid

        _write_pid(12345)
        assert _read_pid() == 12345

    def test_read_missing_returns_none(self, daemon_paths):
        from leashd.daemon import _read_pid

        assert _read_pid() is None

    def test_read_invalid_content_returns_none(self, daemon_paths):
        from leashd.daemon import _read_pid

        pid_file = daemon_paths[0]
        pid_file.write_text("not-a-number")
        assert _read_pid() is None

    def test_remove_existing(self, daemon_paths):
        from leashd.daemon import _read_pid, _remove_pid, _write_pid

        _write_pid(99)
        _remove_pid()
        assert _read_pid() is None

    def test_remove_missing_no_error(self, daemon_paths):
        from leashd.daemon import _remove_pid

        _remove_pid()  # Should not raise

    def test_write_creates_parent_dir(self, tmp_path):
        """_write_pid creates ~/.leashd/ if it doesn't exist."""
        from leashd.daemon import _write_pid

        leashd_dir = tmp_path / "new_dir"
        pid_file = leashd_dir / "leashd.pid"
        with (
            patch("leashd.daemon._LEASHD_DIR", leashd_dir),
            patch("leashd.daemon._PID_FILE", pid_file),
        ):
            _write_pid(42)
            assert pid_file.read_text() == "42"


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        from leashd.daemon import _is_process_alive

        assert _is_process_alive(os.getpid()) is True

    def test_dead_pid(self):
        from leashd.daemon import _is_process_alive

        # PID 2**22 is extremely unlikely to be alive
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _is_process_alive(4194304) is False

    def test_permission_error_means_alive(self):
        from leashd.daemon import _is_process_alive

        with patch("os.kill", side_effect=PermissionError):
            assert _is_process_alive(1) is True


class TestIsRunning:
    def test_no_pid_file(self, daemon_paths):
        from leashd.daemon import is_running

        with patch("leashd.daemon._find_daemon_pid", return_value=None):
            running, pid = is_running()
        assert running is False
        assert pid is None

    def test_live_pid(self, daemon_paths):
        from leashd.daemon import _write_pid, is_running

        _write_pid(os.getpid())
        running, pid = is_running()
        assert running is True
        assert pid == os.getpid()

    def test_stale_pid_auto_cleaned(self, daemon_paths):
        from leashd.daemon import _read_pid, is_running

        pid_file = daemon_paths[0]
        pid_file.write_text("999999")

        with patch("leashd.daemon._is_process_alive", return_value=False):
            running, pid = is_running()
        assert running is False
        assert pid is None
        assert _read_pid() is None  # PID file was cleaned up

    def test_fallback_process_scan(self, daemon_paths):
        """is_running finds daemon via pgrep when PID file is missing."""
        from leashd.daemon import _read_pid, is_running

        with patch("leashd.daemon._find_daemon_pid", return_value=61132):
            running, pid = is_running()
        assert running is True
        assert pid == 61132
        assert _read_pid() == 61132  # PID file self-healed

    def test_fallback_process_scan_no_match(self, daemon_paths):
        """Returns (False, None) when pgrep finds nothing."""
        from leashd.daemon import is_running

        with patch("leashd.daemon._find_daemon_pid", return_value=None):
            running, pid = is_running()
        assert running is False
        assert pid is None


class TestStartDaemon:
    def test_already_running_raises(self, daemon_paths):
        from leashd.daemon import start_daemon

        with (
            patch("leashd.daemon.is_running", return_value=(True, 123)),
            pytest.raises(DaemonError, match="already running"),
        ):
            start_daemon()

    def test_spawns_subprocess(self, daemon_paths):
        from leashd.daemon import _read_pid, start_daemon

        _pid_file, _daemon_log, _leashd_dir = daemon_paths
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.poll.return_value = None

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("time.sleep"),
        ):
            result = start_daemon()

        assert result == 54321
        assert _read_pid() == 54321
        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        assert call_kwargs.kwargs["start_new_session"] is True

    def test_stale_pid_allows_restart(self, daemon_paths):
        from leashd.daemon import start_daemon

        pid_file = daemon_paths[0]
        pid_file.write_text("99999")
        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = None

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("time.sleep"),
        ):
            result = start_daemon()

        assert result == 11111

    def test_child_crash_raises_daemon_error(self, daemon_paths):
        """When child exits immediately, DaemonError is raised with log tail."""
        from leashd.daemon import start_daemon

        _pid_file, daemon_log, _leashd_dir = daemon_paths
        daemon_log.write_text("ImportError: no module named 'foo'")
        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_proc.poll.return_value = 1

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("time.sleep"),
            pytest.raises(DaemonError, match="exited immediately"),
        ):
            start_daemon()

    def test_child_crash_cleans_pid_file(self, daemon_paths):
        """PID file is removed when child crashes on startup."""
        from leashd.daemon import _read_pid, start_daemon

        _pid_file, daemon_log, _leashd_dir = daemon_paths
        daemon_log.write_text("crash")
        mock_proc = MagicMock()
        mock_proc.pid = 55556
        mock_proc.poll.return_value = 1

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("time.sleep"),
            pytest.raises(DaemonError),
        ):
            start_daemon()

        assert _read_pid() is None

    def test_popen_failure_closes_log_file(self, daemon_paths):
        """When Popen raises, the finally block closes the log file descriptor."""
        from leashd.daemon import start_daemon

        mock_log_file = MagicMock()
        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("builtins.open", return_value=mock_log_file),
            patch("subprocess.Popen", side_effect=FileNotFoundError("no python")),
            pytest.raises(FileNotFoundError, match="no python"),
        ):
            start_daemon()

        mock_log_file.close.assert_called_once()

    def test_write_pid_failure_kills_orphan(self, daemon_paths):
        """When _write_pid raises after Popen succeeds, os.kill(SIGTERM) is called."""
        from leashd.daemon import start_daemon

        mock_proc = MagicMock()
        mock_proc.pid = 77777

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("leashd.daemon._write_pid", side_effect=OSError("disk full")),
            patch("os.kill") as mock_kill,
            pytest.raises(OSError, match="disk full"),
        ):
            start_daemon()

        mock_kill.assert_called_once_with(77777, signal.SIGTERM)

    def test_write_pid_failure_kills_orphan_already_dead(self, daemon_paths):
        """When child already exited, ProcessLookupError from os.kill is suppressed."""
        from leashd.daemon import start_daemon

        mock_proc = MagicMock()
        mock_proc.pid = 88888

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("leashd.daemon._write_pid", side_effect=OSError("disk full")),
            patch("os.kill", side_effect=ProcessLookupError),
            pytest.raises(OSError, match="disk full"),
        ):
            start_daemon()


class TestStopDaemon:
    def test_not_running_raises(self, daemon_paths):
        from leashd.daemon import stop_daemon

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            pytest.raises(DaemonError, match="not running"),
        ):
            stop_daemon()

    def test_sends_sigterm_and_waits(self, daemon_paths):
        from leashd.daemon import _write_pid, stop_daemon

        _write_pid(12345)
        call_count = 0

        def mock_alive(pid):
            nonlocal call_count
            call_count += 1
            # Die after second check
            return call_count < 3

        with (
            patch("leashd.daemon.is_running", return_value=(True, 12345)),
            patch("os.kill") as mock_kill,
            patch("leashd.daemon._is_process_alive", side_effect=mock_alive),
            patch("time.sleep"),
        ):
            result = stop_daemon()

        assert result is True
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_timeout_returns_false(self, daemon_paths):
        from leashd.daemon import stop_daemon

        with (
            patch("leashd.daemon.is_running", return_value=(True, 12345)),
            patch("os.kill"),
            patch("leashd.daemon._is_process_alive", return_value=True),
            patch("time.monotonic", side_effect=[0.0, 0.0, 11.0]),
            patch("time.sleep"),
        ):
            result = stop_daemon()

        assert result is False

    def test_stop_process_exits_between_check_and_kill(self, daemon_paths):
        """TOCTOU: process dies after is_running() — ProcessLookupError caught."""
        from leashd.daemon import _write_pid, stop_daemon

        _write_pid(12345)
        pid_file = daemon_paths[0]

        with (
            patch("leashd.daemon.is_running", return_value=(True, 12345)),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            result = stop_daemon()

        assert result is True
        assert not pid_file.exists()


class TestCleanup:
    def test_cleanup_removes_pid_file(self, daemon_paths):
        """Public cleanup() API removes the PID file."""
        from leashd.daemon import _write_pid, cleanup

        pid_file = daemon_paths[0]
        _write_pid(42)
        assert pid_file.exists()

        cleanup()
        assert not pid_file.exists()

    def test_cleanup_idempotent(self, daemon_paths):
        """cleanup() safe to call twice — no error on second call."""
        from leashd.daemon import cleanup

        cleanup()
        cleanup()  # Should not raise
