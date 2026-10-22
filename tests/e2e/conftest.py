"""Shared fixtures for WebUI end-to-end tests.

Each test module gets ONE server + ONE browser (module-scoped).
Each test gets a fresh page + context (function-scoped).
This avoids re-launching Chromium and uvicorn for every single test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import uvicorn

playwright_async_api = pytest.importorskip(
    "playwright.async_api",
    reason="Playwright not installed — skip e2e tests",
)
Page = playwright_async_api.Page
Browser = playwright_async_api.Browser
async_playwright = playwright_async_api.async_playwright

from leashd.core.config import LeashdConfig  # noqa: E402
from leashd.web.app import create_app  # noqa: E402
from leashd.web.models import ServerMessage  # noqa: E402
from leashd.web.ws_handler import WebSocketHandler  # noqa: E402

_API_KEY = "e2e-test-key"


async def _wait_for_server(server: uvicorn.Server, timeout: float = 5.0) -> int:
    """Wait for uvicorn to bind and return the actual port."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if server.started:
            return server.servers[0].sockets[0].getsockname()[1]  # type: ignore[union-attr]
        await asyncio.sleep(0.05)
    raise RuntimeError(f"Server did not start within {timeout}s")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def test_server(tmp_path_factory) -> AsyncGenerator[SimpleNamespace, None]:  # type: ignore[type-arg]
    """Start a real leashd WebUI server on a free port (once per module).

    Yields a SimpleNamespace with:
      - url: str — base HTTP URL
      - api_key: str
      - ws_handler: WebSocketHandler — for injecting server messages
      - chat_ids: list[str] — populated with chat_id on each WebSocket auth
      - received: list[dict] — messages received from the browser
    """
    tmp_path = tmp_path_factory.mktemp("e2e")
    config = LeashdConfig(
        approved_directories=[tmp_path],
        web_enabled=True,
        web_api_key=_API_KEY,
        web_port=0,
        web_host="127.0.0.1",
    )
    ws_handler = WebSocketHandler(api_key=_API_KEY)

    connected_chat_ids: list[str] = []
    ws_handler.set_on_connect(lambda cid: connected_chat_ids.append(cid))

    received: list[dict[str, Any]] = []

    async def _mock_message_handler(
        user_id: str,
        text: str,
        chat_id: str,
        attachments: Any = None,
    ) -> None:
        received.append({"text": text, "chat_id": chat_id})

    ws_handler.set_message_handler(_mock_message_handler)

    app = create_app(config, ws_handler, message_store=None)
    uv_cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(uv_cfg)
    task = asyncio.create_task(server.serve())
    try:
        port = await _wait_for_server(server)
    except Exception:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise

    yield SimpleNamespace(
        url=f"http://127.0.0.1:{port}",
        api_key=_API_KEY,
        ws_handler=ws_handler,
        chat_ids=connected_chat_ids,
        received=received,
    )

    server.should_exit = True
    await asyncio.wait_for(task, timeout=5.0)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def _browser() -> AsyncGenerator[Browser, None]:
    """Launch Chromium once per module (expensive operation)."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    yield browser
    await browser.close()
    await pw.stop()


@pytest.fixture(autouse=True)
def _reset_server_state(test_server: SimpleNamespace) -> None:
    """Clear accumulated state before each test to prevent cross-contamination."""
    test_server.received.clear()


@pytest_asyncio.fixture(loop_scope="module")
async def page(
    test_server: SimpleNamespace, _browser: Browser
) -> AsyncGenerator[Page, None]:
    """Fresh Playwright page pointed at the test server (not yet authenticated)."""
    ctx = await _browser.new_context(base_url=test_server.url)
    pg = await ctx.new_page()
    yield pg
    await ctx.close()


@pytest_asyncio.fixture(loop_scope="module")
async def authed_page(page: Page, test_server: SimpleNamespace) -> Page:
    """Playwright page fixture already authenticated and showing the chat screen."""
    await page.goto(test_server.url)
    await page.fill("#api-key-input", test_server.api_key)
    await page.click("#auth-btn")
    await page.wait_for_selector("#chat-screen:not([hidden])", timeout=6000)
    await page.wait_for_selector(".dot-connected", timeout=6000)
    return page


@pytest_asyncio.fixture(loop_scope="module")
async def chat_id(authed_page: Page, test_server: SimpleNamespace) -> str:
    """Return the WebSocket chat_id for the authenticated session.

    Waits briefly for the on_connect callback to fire after auth.
    """
    for _ in range(50):
        if test_server.chat_ids:
            return test_server.chat_ids[-1]
        await asyncio.sleep(0.05)
    raise RuntimeError("on_connect callback never fired — chat_id unavailable")


@pytest_asyncio.fixture(loop_scope="module")
async def capture(test_server: SimpleNamespace) -> dict[str, list[dict[str, Any]]]:
    """Register capturing resolvers on the WS handler.

    Tests that click Approve/Deny or answer questions use this fixture to assert
    what was sent back to the server.

    Returns a dict with keys: "approvals", "interactions", "interrupts".
    """
    results: dict[str, list[dict[str, Any]]] = {
        "approvals": [],
        "auto_approvals": [],
        "interactions": [],
        "interrupts": [],
    }

    async def _approval(
        approval_id: str, approved: bool, approver_id: str | None = None
    ) -> bool:
        results["approvals"].append({"approval_id": approval_id, "approved": approved})
        return True

    def _auto_approve(chat_id: str, tool_name: str) -> None:
        results["auto_approvals"].append({"chat_id": chat_id, "tool_name": tool_name})

    async def _interaction(interaction_id: str, answer: str) -> bool:
        results["interactions"].append(
            {"interaction_id": interaction_id, "answer": answer}
        )
        return True

    async def _interrupt(interrupt_id: str, send_now: bool) -> bool:
        results["interrupts"].append(
            {"interrupt_id": interrupt_id, "send_now": send_now}
        )
        return True

    test_server.ws_handler.set_approval_resolver(_approval)
    test_server.ws_handler.set_auto_approve_handler(_auto_approve)
    test_server.ws_handler.set_interaction_resolver(_interaction)
    test_server.ws_handler.set_interrupt_resolver(_interrupt)

    return results


async def inject(
    ws_handler: WebSocketHandler,
    chat_ids: list[str],
    msg_type: str,
    payload: dict[str, Any],
) -> None:
    """Helper to send a ServerMessage to the connected browser."""
    if not chat_ids:
        raise RuntimeError(
            "No connected chat_id — ensure authed_page fixture is used first"
        )
    await ws_handler.send_to(
        chat_ids[-1], ServerMessage(type=msg_type, payload=payload)
    )  # type: ignore[arg-type]


async def wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
    interval: float = 0.05,
    msg: str = "Condition not met within timeout",
) -> None:
    """Poll *predicate* until it returns True or *timeout* seconds elapse."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"{msg} (waited {timeout}s)")
