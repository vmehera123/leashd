"""Tunnel process management for exposing the WebUI publicly."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from leashd.exceptions import TunnelError

_PROVIDERS: dict[str, dict[str, object]] = {
    "ngrok": {
        "binary": "ngrok",
        "build_cmd": lambda port: ["ngrok", "http", str(port)],
        "timeout": 15,
    },
    "cloudflare": {
        "binary": "cloudflared",
        "build_cmd": lambda port: [
            "cloudflared",
            "tunnel",
            "--url",
            f"http://localhost:{port}",
        ],
        "timeout": 30,
    },
    "tailscale": {
        "binary": "tailscale",
        "build_cmd": lambda port: ["tailscale", "funnel", str(port)],
        "timeout": 15,
    },
}


def _parse_ngrok_url(port: int, timeout: float) -> str:  # noqa: ARG001
    """Poll ngrok's local API to get the public URL."""
    api_url = "http://127.0.0.1:4040/api/tunnels"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(api_url, timeout=2) as resp:  # noqa: S310
                data = json.loads(resp.read())
                for tunnel in data.get("tunnels", []):
                    public_url: str = tunnel.get("public_url", "")
                    if public_url.startswith("https://"):
                        return public_url
        except Exception:  # noqa: S110
            pass
        time.sleep(1)
    raise TunnelError("Could not retrieve ngrok public URL within timeout")


def _parse_cloudflared_url(proc: subprocess.Popen[bytes], timeout: float) -> str:
    """Read cloudflared stderr for the public URL."""
    pattern = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
    deadline = time.monotonic() + timeout
    stderr = proc.stderr
    if stderr is None:
        raise TunnelError("cloudflared stderr not available")
    while time.monotonic() < deadline:
        line = stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise TunnelError("cloudflared exited before producing a URL")
            continue
        match = pattern.search(line.decode("utf-8", errors="replace"))
        if match:
            return match.group(1)
    raise TunnelError("Could not retrieve cloudflared URL within timeout")


def _parse_tailscale_url(proc: subprocess.Popen[bytes], timeout: float) -> str:
    """Read tailscale funnel output for the public URL."""
    pattern = re.compile(r"(https://[a-z0-9.-]+\.ts\.net\S*)")
    deadline = time.monotonic() + timeout
    stderr = proc.stderr
    if stderr is None:
        raise TunnelError("tailscale stderr not available")
    while time.monotonic() < deadline:
        line = stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise TunnelError("tailscale exited before producing a URL")
            continue
        match = pattern.search(line.decode("utf-8", errors="replace"))
        if match:
            return match.group(1)
    raise TunnelError("Could not retrieve tailscale URL within timeout")


class TunnelProcess:
    """Manages a tunnel subprocess lifecycle."""

    def __init__(self, provider: str, port: int) -> None:
        if provider not in _PROVIDERS:
            raise TunnelError(f"Unknown provider: {provider}")
        self._provider = provider
        self._port = port
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> str:
        """Start the tunnel subprocess and return the public URL."""
        info = _PROVIDERS[self._provider]
        binary = str(info["binary"])
        timeout = float(info["timeout"])  # type: ignore[arg-type]

        if not shutil.which(binary):
            raise TunnelError(
                f"'{binary}' not found in PATH. "
                f"Install it before using --provider {self._provider}."
            )

        build_cmd = info["build_cmd"]
        if not callable(build_cmd):
            raise TunnelError(f"Invalid provider config for {self._provider}")
        cmd: list[str] = build_cmd(self._port)

        if self._provider == "ngrok":
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True
            )
            self._stderr_thread.start()
        else:
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if self._proc.poll() is not None:
            raise TunnelError(f"{binary} exited immediately after start")

        if self._provider == "ngrok":
            return _parse_ngrok_url(self._port, timeout)
        if self._provider == "cloudflare":
            return _parse_cloudflared_url(self._proc, timeout)
        return _parse_tailscale_url(self._proc, timeout)

    def stop(self) -> None:
        """Stop the tunnel subprocess."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)

    @property
    def is_alive(self) -> bool:
        """Check if the tunnel process is still running."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        """Return the process exit code, or None if still running."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def get_stderr(self) -> str:
        """Return captured stderr output (if any)."""
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        return "\n".join(self._stderr_lines)

    def _drain_stderr(self) -> None:
        """Read stderr lines into a buffer (runs in a background thread)."""
        if self._proc is None or self._proc.stderr is None:
            return
        for raw_line in self._proc.stderr:
            self._stderr_lines.append(
                raw_line.decode("utf-8", errors="replace").rstrip()
            )
        self._proc.stderr.close()


def notify_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a one-shot message via Telegram Bot HTTP API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except Exception:
        return False
