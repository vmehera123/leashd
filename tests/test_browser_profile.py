"""Persistent browser profile hygiene."""

import socket

from leashd.browser_profile import (
    merged_launch_args,
    profile_in_use,
    prune_restore_state,
)


def _make_profile(root, *, windows: int = 3):
    sessions = root / "Default" / "Sessions"
    sessions.mkdir(parents=True)
    for i in range(windows):
        (sessions / f"Session_1342970453387426{i}").write_bytes(b"window-state")
        (sessions / f"Tabs_1342970451493805{i}").write_bytes(b"tab-state")
    return sessions


class TestPruneRestoreState:
    """Chrome replays every saved window on launch.

    Regression: a `/web` run relaunched the browser per URL, leaking a window
    into the profile's restore state each time, until a single launch restored
    ~200 blank windows onto the user's screen.
    """

    def test_removes_saved_windows(self, tmp_path):
        sessions = _make_profile(tmp_path)

        assert prune_restore_state(tmp_path) == 6
        assert list(sessions.iterdir()) == []

    def test_preserves_cookies_and_storage(self, tmp_path):
        _make_profile(tmp_path)
        default = tmp_path / "Default"
        (default / "Cookies").write_bytes(b"auth")
        (default / "Preferences").write_text("{}")
        storage = default / "Local Storage"
        storage.mkdir()
        (storage / "leveldb").write_bytes(b"tokens")

        prune_restore_state(tmp_path)

        assert (default / "Cookies").read_bytes() == b"auth"
        assert (default / "Preferences").read_text() == "{}"
        assert (storage / "leveldb").read_bytes() == b"tokens"

    def test_skips_while_profile_in_use(self, tmp_path):
        sessions = _make_profile(tmp_path)
        (tmp_path / "SingletonLock").write_text("host-1234")

        assert prune_restore_state(tmp_path) == 0
        assert len(list(sessions.iterdir())) == 6

    def test_covers_every_profile_directory(self, tmp_path):
        _make_profile(tmp_path, windows=1)
        other = tmp_path / "Profile 2" / "Sessions"
        other.mkdir(parents=True)
        (other / "Session_99").write_bytes(b"state")

        assert prune_restore_state(tmp_path) == 3
        assert list(other.iterdir()) == []

    def test_leaves_unrelated_session_files(self, tmp_path):
        sessions = _make_profile(tmp_path, windows=1)
        (sessions / "README").write_text("keep")

        prune_restore_state(tmp_path)

        assert (sessions / "README").read_text() == "keep"

    def test_missing_profile_is_noop(self, tmp_path):
        assert prune_restore_state(tmp_path / "absent") == 0

    def test_profile_without_sessions_dir(self, tmp_path):
        (tmp_path / "Default").mkdir()

        assert prune_restore_state(tmp_path) == 0

    def test_idempotent(self, tmp_path):
        _make_profile(tmp_path)

        assert prune_restore_state(tmp_path) == 6
        assert prune_restore_state(tmp_path) == 0


class TestProfileInUse:
    def test_detects_lock(self, tmp_path):
        assert profile_in_use(tmp_path) is False
        (tmp_path / "SingletonLock").write_text("host-1234")
        assert profile_in_use(tmp_path) is True

    def test_detects_socket(self, tmp_path):
        (tmp_path / "SingletonSocket").write_text("")
        assert profile_in_use(tmp_path) is True

    def test_dead_devtools_port_outranks_stale_socket(self, tmp_path):
        """Chrome leaves SingletonSocket behind on an unclean exit, and its
        /var/folders target outlives the process. Trusting the lock files alone
        reported a long-dead browser as live, so the prune below never ran and
        the previous run's tabs kept coming back."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        (tmp_path / "SingletonSocket").write_text("")
        (tmp_path / "DevToolsActivePort").write_text(
            f"{dead_port}\n/devtools/browser/x"
        )

        assert profile_in_use(tmp_path) is False

    def test_live_devtools_port_reports_in_use(self, tmp_path):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            (tmp_path / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x")

            assert profile_in_use(tmp_path) is True

    def test_unreadable_port_falls_back_to_locks(self, tmp_path):
        (tmp_path / "DevToolsActivePort").write_text("not-a-port\n")
        assert profile_in_use(tmp_path) is False
        (tmp_path / "SingletonLock").write_text("host-1234")
        assert profile_in_use(tmp_path) is True

    def test_prune_runs_once_the_port_is_dead(self, tmp_path):
        sessions = _make_profile(tmp_path)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        (tmp_path / "SingletonSocket").write_text("")
        (tmp_path / "DevToolsActivePort").write_text(f"{dead_port}\n")

        assert prune_restore_state(tmp_path) == 6
        assert list(sessions.iterdir()) == []


class TestMergedLaunchArgs:
    """Headed Chrome opens its own startup window alongside agent-browser's.

    Regression: that left a `chrome://newtab/` page per launch, loading the
    Google new-tab widgets. `agent-browser tab` is window-scoped and never
    reported it, so nothing closed it either.
    """

    def test_adds_no_startup_window(self):
        assert merged_launch_args(None) == "--no-startup-window"
        assert merged_launch_args("") == "--no-startup-window"

    def test_preserves_user_args(self):
        merged = merged_launch_args("--no-sandbox,--mute-audio")

        assert merged.split(",") == [
            "--no-sandbox",
            "--mute-audio",
            "--no-startup-window",
        ]

    def test_does_not_duplicate(self):
        assert merged_launch_args("--no-startup-window") == "--no-startup-window"
        assert (
            merged_launch_args("--no-startup-window,--no-sandbox")
            == "--no-startup-window,--no-sandbox"
        )
