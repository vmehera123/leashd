"""Web Push notification service — VAPID key management, subscription storage, push delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import structlog
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

logger = structlog.get_logger()

_LEASHD_DIR = Path.home() / ".leashd"
_VAPID_KEYS_PATH = _LEASHD_DIR / "vapid_keys.json"
_SUBSCRIPTIONS_PATH = _LEASHD_DIR / "push_subscriptions.json"


def _ensure_vapid_keys() -> dict[str, str]:
    """Load or generate VAPID key pair. Keys are stored in ~/.leashd/vapid_keys.json."""
    if _VAPID_KEYS_PATH.exists():
        try:
            data: dict[str, str] = json.loads(_VAPID_KEYS_PATH.read_text())
            if data.get("private_key") and data.get("public_key"):
                return data
        except (json.JSONDecodeError, KeyError):
            logger.warning("vapid_keys_corrupt", path=str(_VAPID_KEYS_PATH))

    vapid = Vapid()
    vapid.generate_keys()

    private_pem = vapid.private_pem()
    private_key = (
        private_pem.decode() if isinstance(private_pem, bytes) else private_pem
    )

    raw_public = vapid.public_key.public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    public_key = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()

    keys = {"private_key": private_key, "public_key": public_key}
    _LEASHD_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=_LEASHD_DIR, suffix=".tmp")
    try:
        os.write(fd, json.dumps(keys, indent=2).encode())
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, _VAPID_KEYS_PATH)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        Path(tmp_path).unlink(missing_ok=True)
        raise
    logger.info("vapid_keys_generated", path=str(_VAPID_KEYS_PATH))
    return keys


class PushService:
    """Manages push subscriptions and sends Web Push notifications."""

    def __init__(self, vapid_contact: str = "") -> None:
        self._keys = _ensure_vapid_keys()
        self._vapid_contact = vapid_contact or "mailto:noreply@leashd.dev"
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._load_subscriptions()
        # Pre-create Vapid instance from PEM — pywebpush's from_string()
        # can't parse PEM format, only raw keys
        self._vapid = Vapid.from_pem(self._keys["private_key"].encode())

    @property
    def public_key(self) -> str:
        return self._keys["public_key"]

    def _load_subscriptions(self) -> None:
        if not _SUBSCRIPTIONS_PATH.exists():
            return
        try:
            data: dict[str, dict[str, Any]] = json.loads(
                _SUBSCRIPTIONS_PATH.read_text()
            )
            self._subscriptions = data
        except (json.JSONDecodeError, KeyError):
            logger.warning("push_subscriptions_corrupt", path=str(_SUBSCRIPTIONS_PATH))

    def _save_subscriptions(self) -> None:
        _LEASHD_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=_LEASHD_DIR, suffix=".tmp")
        try:
            os.write(fd, json.dumps(self._subscriptions, indent=2).encode())
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, _SUBSCRIPTIONS_PATH)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def subscribe(self, chat_id: str, subscription: dict[str, Any]) -> None:
        self._subscriptions[chat_id] = subscription
        self._save_subscriptions()
        logger.info("push_subscribed", chat_id=chat_id)

    def unsubscribe(self, chat_id: str) -> None:
        if self._subscriptions.pop(chat_id, None) is not None:
            self._save_subscriptions()
            logger.info("push_unsubscribed", chat_id=chat_id)

    def has_subscription(self, chat_id: str) -> bool:
        return chat_id in self._subscriptions

    async def send_push(
        self,
        chat_id: str,
        *,
        title: str,
        body: str,
        event_type: str = "",
        url: str = "/",
    ) -> bool:
        sub = self._subscriptions.get(chat_id)
        if not sub:
            return False

        payload = json.dumps(
            {"title": title, "body": body, "event_type": event_type, "url": url}
        )

        urgent = event_type in ("approval_request", "question", "interrupt_prompt")
        headers = {
            "Urgency": "high" if urgent else "normal",
            "Topic": event_type or "leashd",
        }

        ttl = 14400 if urgent else 86400

        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=sub,
                data=payload,
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self._vapid_contact},
                ttl=ttl,
                headers=headers,
            )
            logger.info(
                "push_sent", chat_id=chat_id, event_type=event_type, title=title
            )
            return True
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                logger.info(
                    "push_subscription_expired",
                    chat_id=chat_id,
                    status=status_code,
                )
                self.unsubscribe(chat_id)
            else:
                logger.warning("push_send_failed", chat_id=chat_id, error=str(exc))
            return False
        except Exception as exc:
            logger.warning("push_send_failed", chat_id=chat_id, error=str(exc))
            return False
