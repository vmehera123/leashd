"""Tests for leashd.web.push — PushService and push REST endpoints."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from leashd.core.config import LeashdConfig
from leashd.web.push import PushService, _ensure_vapid_keys
from leashd.web.routes import create_rest_router

_AUTH_HEADER = {"X-API-Key": "test-key-123"}


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        web_enabled=True,
        web_api_key="test-key-123",
    )


@pytest.fixture
def vapid_keys(tmp_path):
    keys = {"private_key": "fake-private-pem", "public_key": "fake-public-b64"}
    keys_path = tmp_path / "vapid_keys.json"
    keys_path.write_text(json.dumps(keys))
    return keys_path, keys


@pytest.fixture
def push_service(tmp_path, vapid_keys, monkeypatch):
    keys_path, _ = vapid_keys
    subs_path = tmp_path / "push_subscriptions.json"
    monkeypatch.setattr("leashd.web.push._VAPID_KEYS_PATH", keys_path)
    monkeypatch.setattr("leashd.web.push._SUBSCRIPTIONS_PATH", subs_path)
    monkeypatch.setattr("leashd.web.push._LEASHD_DIR", tmp_path)
    with patch("leashd.web.push.Vapid") as mock_vapid_cls:
        mock_vapid_cls.from_pem.return_value = MagicMock()
        return PushService()


@pytest.fixture
def client_with_push(config, push_service):
    app = FastAPI()
    router = create_rest_router(config, None, push_service)
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def client_no_push(config):
    app = FastAPI()
    router = create_rest_router(config, None, None)
    app.include_router(router)
    return TestClient(app)


class TestEnsureVapidKeys:
    def test_generates_keys_when_missing(self, tmp_path, monkeypatch):
        keys_path = tmp_path / "vapid_keys.json"
        monkeypatch.setattr("leashd.web.push._VAPID_KEYS_PATH", keys_path)
        monkeypatch.setattr("leashd.web.push._LEASHD_DIR", tmp_path)

        result = _ensure_vapid_keys()

        assert keys_path.exists()
        assert "private_key" in result
        assert "public_key" in result
        stored = json.loads(keys_path.read_text())
        assert stored["private_key"] == result["private_key"]

    def test_loads_existing_keys(self, tmp_path, monkeypatch):
        keys_path = tmp_path / "vapid_keys.json"
        expected = {"private_key": "test-private", "public_key": "test-public"}
        keys_path.write_text(json.dumps(expected))
        monkeypatch.setattr("leashd.web.push._VAPID_KEYS_PATH", keys_path)
        monkeypatch.setattr("leashd.web.push._LEASHD_DIR", tmp_path)

        result = _ensure_vapid_keys()

        assert result == expected

    def test_regenerates_on_corrupt_keys(self, tmp_path, monkeypatch):
        keys_path = tmp_path / "vapid_keys.json"
        keys_path.write_text("not-json")
        monkeypatch.setattr("leashd.web.push._VAPID_KEYS_PATH", keys_path)
        monkeypatch.setattr("leashd.web.push._LEASHD_DIR", tmp_path)

        result = _ensure_vapid_keys()

        assert "private_key" in result
        assert "public_key" in result


class TestPushService:
    def test_public_key(self, push_service):
        assert push_service.public_key == "fake-public-b64"

    def test_subscribe_and_unsubscribe(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:abc", sub)

        assert push_service.has_subscription("web:abc")

        push_service.unsubscribe("web:abc")
        assert not push_service.has_subscription("web:abc")

    def test_subscriptions_persist_to_disk(self, push_service, tmp_path, monkeypatch):
        subs_path = tmp_path / "push_subscriptions.json"
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:test", sub)

        assert subs_path.exists()
        data = json.loads(subs_path.read_text())
        assert "web:test" in data
        assert data["web:test"]["endpoint"] == "https://example.com/push"

    def test_unsubscribe_nonexistent_is_noop(self, push_service):
        push_service.unsubscribe("web:doesnotexist")
        assert not push_service.has_subscription("web:doesnotexist")

    async def test_send_push_no_subscription(self, push_service):
        result = await push_service.send_push(
            "web:unknown", title="Test", body="Hello", event_type="test"
        )
        assert result is False

    async def test_send_push_success(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:ok", sub)

        with patch("leashd.web.push.webpush") as mock_wp:
            result = await push_service.send_push(
                "web:ok", title="Test", body="Hello", event_type="test"
            )

        assert result is True
        mock_wp.assert_called_once()
        call_kwargs = mock_wp.call_args[1]
        assert call_kwargs["ttl"] == 86400
        assert call_kwargs["headers"]["Topic"] == "test"
        assert call_kwargs["headers"]["Urgency"] == "normal"

    async def test_send_push_urgent_events(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:urgent", sub)

        for event_type in ("approval_request", "question", "interrupt_prompt"):
            with patch("leashd.web.push.webpush") as mock_wp:
                await push_service.send_push(
                    "web:urgent",
                    title="Test",
                    body="Hello",
                    event_type=event_type,
                )
            call_kwargs = mock_wp.call_args[1]
            assert call_kwargs["headers"]["Urgency"] == "high", event_type
            assert call_kwargs["ttl"] == 14400, event_type

    async def test_send_push_payload_json_structure(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:payload", sub)

        with patch("leashd.web.push.webpush") as mock_wp:
            await push_service.send_push(
                "web:payload",
                title="Approval Required",
                body="Bash: pip install numpy",
                event_type="approval_request",
                url="/custom",
            )

        call_kwargs = mock_wp.call_args[1]
        payload = json.loads(call_kwargs["data"])
        assert payload["title"] == "Approval Required"
        assert payload["body"] == "Bash: pip install numpy"
        assert payload["event_type"] == "approval_request"
        assert payload["url"] == "/custom"

    async def test_send_push_url_parameter_forwarded(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:url", sub)

        with patch("leashd.web.push.webpush") as mock_wp:
            await push_service.send_push(
                "web:url",
                title="Test",
                body="Hello",
                event_type="test",
                url="/deep/link",
            )

        payload = json.loads(mock_wp.call_args[1]["data"])
        assert payload["url"] == "/deep/link"

    async def test_send_push_default_url(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:defurl", sub)

        with patch("leashd.web.push.webpush") as mock_wp:
            await push_service.send_push(
                "web:defurl",
                title="Test",
                body="Hello",
                event_type="test",
            )

        payload = json.loads(mock_wp.call_args[1]["data"])
        assert payload["url"] == "/"

    async def test_send_push_404_auto_unsubscribes(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:404", sub)

        mock_response = MagicMock()
        mock_response.status_code = 404
        exc = WebPushException("Not Found", response=mock_response)

        with patch("leashd.web.push.webpush", side_effect=exc):
            result = await push_service.send_push(
                "web:404", title="Test", body="Hello", event_type="test"
            )

        assert result is False
        assert not push_service.has_subscription("web:404")

    async def test_send_push_non_webpush_exception(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:conn", sub)

        with patch(
            "leashd.web.push.webpush", side_effect=ConnectionError("network down")
        ):
            result = await push_service.send_push(
                "web:conn", title="Test", body="Hello", event_type="test"
            )

        assert result is False
        # Subscription should NOT be removed for non-WebPush errors
        assert push_service.has_subscription("web:conn")

    async def test_send_push_500_does_not_remove_subscription(self, push_service):
        """Server errors (500, 429) should NOT remove the subscription — only 404/410 do."""
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:500", sub)

        for status_code in (500, 429, 503):
            mock_response = MagicMock()
            mock_response.status_code = status_code
            exc = WebPushException("Server Error", response=mock_response)

            with patch("leashd.web.push.webpush", side_effect=exc):
                result = await push_service.send_push(
                    "web:500", title="Test", body="Hello", event_type="test"
                )

            assert result is False
            assert push_service.has_subscription("web:500"), (
                f"Subscription removed on {status_code} — should only remove on 404/410"
            )

    async def test_send_push_expired_subscription(self, push_service):
        sub = {
            "endpoint": "https://example.com/push",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        push_service.subscribe("web:expired", sub)

        mock_response = MagicMock()
        mock_response.status_code = 410
        exc = WebPushException("Gone", response=mock_response)

        with patch("leashd.web.push.webpush", side_effect=exc):
            result = await push_service.send_push(
                "web:expired", title="Test", body="Hello", event_type="test"
            )

        assert result is False
        assert not push_service.has_subscription("web:expired")

    def test_loads_subscriptions_from_disk(self, tmp_path, monkeypatch):
        keys_path = tmp_path / "vapid_keys.json"
        keys_path.write_text(json.dumps({"private_key": "p", "public_key": "k"}))
        subs_path = tmp_path / "push_subscriptions.json"
        sub_data = {
            "web:loaded": {
                "endpoint": "https://example.com",
                "keys": {"p256dh": "x", "auth": "y"},
            }
        }
        subs_path.write_text(json.dumps(sub_data))
        monkeypatch.setattr("leashd.web.push._VAPID_KEYS_PATH", keys_path)
        monkeypatch.setattr("leashd.web.push._SUBSCRIPTIONS_PATH", subs_path)
        monkeypatch.setattr("leashd.web.push._LEASHD_DIR", tmp_path)

        with patch("leashd.web.push.Vapid") as mock_vapid_cls:
            mock_vapid_cls.from_pem.return_value = MagicMock()
            svc = PushService()
        assert svc.has_subscription("web:loaded")


class TestPushEndpoints:
    def test_vapid_key_with_push_service(self, client_with_push):
        resp = client_with_push.get("/api/push/vapid-key", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["public_key"] == "fake-public-b64"

    def test_vapid_key_without_push_service(self, client_no_push):
        resp = client_no_push.get("/api/push/vapid-key", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["public_key"] == ""

    def test_vapid_key_requires_auth(self, client_with_push):
        resp = client_with_push.get("/api/push/vapid-key")
        assert resp.status_code == 401

    def test_subscribe(self, client_with_push, push_service):
        resp = client_with_push.post(
            "/api/push/subscribe",
            json={
                "subscription": {"endpoint": "https://e.co", "keys": {}},
                "chat_id": "web:s1",
            },
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert push_service.has_subscription("web:s1")

    def test_subscribe_requires_fields(self, client_with_push):
        resp = client_with_push.post(
            "/api/push/subscribe",
            json={"subscription": None, "chat_id": ""},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 400

    def test_subscribe_without_push_service(self, client_no_push):
        resp = client_no_push.post(
            "/api/push/subscribe",
            json={"subscription": {"endpoint": "x"}, "chat_id": "web:s1"},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 501

    def test_unsubscribe(self, client_with_push, push_service):
        push_service.subscribe("web:rm", {"endpoint": "https://e.co", "keys": {}})
        resp = client_with_push.request(
            "DELETE",
            "/api/push/subscribe",
            json={"chat_id": "web:rm"},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert not push_service.has_subscription("web:rm")

    def test_unsubscribe_requires_auth(self, client_with_push):
        resp = client_with_push.request(
            "DELETE",
            "/api/push/subscribe",
            json={"chat_id": "web:rm"},
        )
        assert resp.status_code == 401

    def test_push_test_endpoint_success(self, client_with_push, push_service):
        push_service.subscribe(
            "web:test-push",
            {"endpoint": "https://e.co", "keys": {"p256dh": "a", "auth": "b"}},
        )
        with patch.object(push_service, "send_push", return_value=True) as mock_send:
            resp = client_with_push.post(
                "/api/push/test",
                json={"chat_id": "web:test-push"},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_send.assert_called_once()

    def test_push_test_endpoint_missing_chat_id(self, client_with_push):
        resp = client_with_push.post(
            "/api/push/test",
            json={},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 400
        assert "chat_id" in resp.json()["error"]

    def test_push_test_endpoint_no_push_service(self, client_no_push):
        resp = client_no_push.post(
            "/api/push/test",
            json={"chat_id": "web:1"},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 501
