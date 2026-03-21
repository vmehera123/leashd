"""Tests for leashd.tunnel — tunnel process management."""

import io
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from leashd.exceptions import TunnelError
from leashd.tunnel import (
    TunnelProcess,
    _parse_cloudflared_url,
    _parse_ngrok_url,
    _parse_tailscale_url,
    notify_telegram,
)


class TestTunnelProcess:
    def test_unknown_provider(self):
        with pytest.raises(TunnelError, match="Unknown provider"):
            TunnelProcess("invalid", 8080)

    def test_binary_not_found(self):
        with patch("leashd.tunnel.shutil.which", return_value=None):
            tunnel = TunnelProcess("ngrok", 8080)
            with pytest.raises(TunnelError, match="not found in PATH"):
                tunnel.start()

    def test_ngrok_start(self):
        tunnels_json = json.dumps(
            {"tunnels": [{"public_url": "https://abc.ngrok.io"}]}
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = tunnels_json
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("leashd.tunnel.shutil.which", return_value="/usr/bin/ngrok"),
            patch("leashd.tunnel.subprocess.Popen") as mock_popen,
            patch("leashd.tunnel.urllib.request.urlopen", return_value=mock_resp),
        ):
            mock_proc = mock_popen.return_value
            mock_proc.poll.return_value = None

            tunnel = TunnelProcess("ngrok", 8080)
            url = tunnel.start()

        assert url == "https://abc.ngrok.io"

    def test_cloudflare_start(self):
        stderr_data = (
            b"INFO Starting tunnel\n"
            b"INFO +---\n"
            b"INFO |  https://abc-def.trycloudflare.com\n"
        )
        mock_stderr = io.BytesIO(stderr_data)

        with (
            patch("leashd.tunnel.shutil.which", return_value="/usr/bin/cloudflared"),
            patch("leashd.tunnel.subprocess.Popen") as mock_popen,
        ):
            mock_proc = mock_popen.return_value
            mock_proc.poll.return_value = None
            mock_proc.stderr = mock_stderr
            mock_proc.stdout = io.BytesIO(b"")

            tunnel = TunnelProcess("cloudflare", 8080)
            url = tunnel.start()

        assert url == "https://abc-def.trycloudflare.com"

    def test_tailscale_start(self):
        stderr_data = b"Available on the internet:\nhttps://myhost.tail1234.ts.net/\n"
        mock_stderr = io.BytesIO(stderr_data)

        with (
            patch("leashd.tunnel.shutil.which", return_value="/usr/bin/tailscale"),
            patch("leashd.tunnel.subprocess.Popen") as mock_popen,
        ):
            mock_proc = mock_popen.return_value
            mock_proc.poll.return_value = None
            mock_proc.stderr = mock_stderr
            mock_proc.stdout = io.BytesIO(b"")

            tunnel = TunnelProcess("tailscale", 8080)
            url = tunnel.start()

        assert url == "https://myhost.tail1234.ts.net/"

    def test_stop_terminates_process(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        tunnel._proc = mock_proc

        tunnel.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=5)

    def test_stop_kills_on_timeout(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]
        tunnel._proc = mock_proc

        tunnel.stop()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_stop_noop_when_not_started(self):
        tunnel = TunnelProcess("ngrok", 8080)
        tunnel.stop()

    def test_stop_noop_when_already_exited(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        tunnel._proc = mock_proc

        tunnel.stop()
        mock_proc.terminate.assert_not_called()

    def test_is_alive_true(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        tunnel._proc = mock_proc

        assert tunnel.is_alive is True

    def test_is_alive_false_no_proc(self):
        tunnel = TunnelProcess("ngrok", 8080)
        assert tunnel.is_alive is False

    def test_is_alive_false_exited(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        tunnel._proc = mock_proc

        assert tunnel.is_alive is False

    def test_process_exits_immediately(self):
        with (
            patch("leashd.tunnel.shutil.which", return_value="/usr/bin/ngrok"),
            patch("leashd.tunnel.subprocess.Popen") as mock_popen,
        ):
            mock_proc = mock_popen.return_value
            mock_proc.poll.return_value = 1

            tunnel = TunnelProcess("ngrok", 8080)
            with pytest.raises(TunnelError, match="exited immediately"):
                tunnel.start()

    def test_exit_code_none_when_not_started(self):
        tunnel = TunnelProcess("ngrok", 8080)
        assert tunnel.exit_code is None

    def test_exit_code_none_when_running(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        tunnel._proc = mock_proc
        assert tunnel.exit_code is None

    def test_exit_code_returns_value(self):
        tunnel = TunnelProcess("ngrok", 8080)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 2
        tunnel._proc = mock_proc
        assert tunnel.exit_code == 2

    def test_get_stderr_empty(self):
        tunnel = TunnelProcess("ngrok", 8080)
        assert tunnel.get_stderr() == ""

    def test_get_stderr_with_content(self):
        tunnel = TunnelProcess("ngrok", 8080)
        tunnel._stderr_lines = ["auth failed", "session expired"]
        assert tunnel.get_stderr() == "auth failed\nsession expired"

    def test_ngrok_start_captures_stderr(self):
        with (
            patch("leashd.tunnel.shutil.which", return_value="/usr/bin/ngrok"),
            patch("leashd.tunnel.subprocess.Popen") as mock_popen,
            patch("leashd.tunnel._parse_ngrok_url", return_value="https://x.ngrok.io"),
        ):
            mock_proc = mock_popen.return_value
            mock_proc.poll.return_value = None
            mock_proc.stderr = io.BytesIO(b"")

            tunnel = TunnelProcess("ngrok", 8080)
            tunnel.start()

            call_kwargs = mock_popen.call_args[1]
            assert call_kwargs["stderr"] == subprocess.PIPE
            assert tunnel._stderr_thread is not None


class TestParseNgrokUrl:
    def test_success(self):
        tunnels_json = json.dumps(
            {"tunnels": [{"public_url": "https://abc.ngrok.io"}]}
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = tunnels_json
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("leashd.tunnel.urllib.request.urlopen", return_value=mock_resp):
            url = _parse_ngrok_url(8080, timeout=2)

        assert url == "https://abc.ngrok.io"

    def test_skips_http_url(self):
        tunnels_json = json.dumps(
            {
                "tunnels": [
                    {"public_url": "http://abc.ngrok.io"},
                    {"public_url": "https://abc.ngrok.io"},
                ]
            }
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = tunnels_json
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("leashd.tunnel.urllib.request.urlopen", return_value=mock_resp):
            url = _parse_ngrok_url(8080, timeout=2)

        assert url == "https://abc.ngrok.io"

    def test_timeout(self):
        with (
            patch(
                "leashd.tunnel.urllib.request.urlopen", side_effect=Exception("refused")
            ),
            pytest.raises(TunnelError, match="Could not retrieve ngrok"),
        ):
            _parse_ngrok_url(8080, timeout=0.1)


class TestParseCloudflaredUrl:
    def test_success(self):
        stderr_data = (
            b"2026-03-18 INFO Starting tunnel\n"
            b"2026-03-18 INFO https://abc-def.trycloudflare.com\n"
        )
        mock_proc = MagicMock()
        mock_proc.stderr = io.BytesIO(stderr_data)
        mock_proc.poll.return_value = None

        url = _parse_cloudflared_url(mock_proc, timeout=5)
        assert url == "https://abc-def.trycloudflare.com"

    def test_process_exits(self):
        mock_proc = MagicMock()
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = 1

        with pytest.raises(TunnelError, match="cloudflared exited"):
            _parse_cloudflared_url(mock_proc, timeout=5)


class TestParseTailscaleUrl:
    def test_success(self):
        stderr_data = b"https://myhost.tail123.ts.net/\n"
        mock_proc = MagicMock()
        mock_proc.stderr = io.BytesIO(stderr_data)
        mock_proc.poll.return_value = None

        url = _parse_tailscale_url(mock_proc, timeout=5)
        assert url == "https://myhost.tail123.ts.net/"

    def test_process_exits(self):
        mock_proc = MagicMock()
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = 1

        with pytest.raises(TunnelError, match="tailscale exited"):
            _parse_tailscale_url(mock_proc, timeout=5)


class TestNotifyTelegram:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("leashd.tunnel.urllib.request.urlopen", return_value=mock_resp):
            result = notify_telegram("token", "123", "hello")

        assert result is True

    def test_failure(self):
        with patch(
            "leashd.tunnel.urllib.request.urlopen", side_effect=Exception("timeout")
        ):
            result = notify_telegram("token", "123", "hello")

        assert result is False
