"""E2E tests for the WebUI approval card system."""

import asyncio

import pytest
from playwright.async_api import Page

from tests.e2e.conftest import SimpleNamespace, inject, wait_for


def _approval_payload(request_id: str = "ap-1") -> dict:
    return {
        "request_id": request_id,
        "tool": "Bash",
        "description": "run tests",
    }


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.e2e
class TestApprovalCards:
    async def test_approval_card_renders(
        self, authed_page: Page, test_server: SimpleNamespace
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-render"),
        )
        card_sel = '[data-approval-id="ap-render"]'
        await authed_page.wait_for_selector(card_sel, timeout=5000)

        tool_text = await authed_page.text_content(f"{card_sel} .approval-tool")
        assert tool_text == "Bash"
        desc_text = await authed_page.text_content(f"{card_sel} .approval-desc")
        assert desc_text == "run tests"

    async def test_approve_button_sends_correct_response(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-approve"),
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-approve"]', timeout=5000
        )
        await authed_page.click(
            '[data-approval-id="ap-approve"] [data-action="approve"]'
        )

        await wait_for(
            lambda: bool(capture["approvals"]),
            msg="No approval response captured",
        )
        resp = capture["approvals"][0]
        assert resp["approval_id"] == "ap-approve"
        assert resp["approved"] is True

    async def test_deny_button_sends_correct_response(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-deny"),
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-deny"]', timeout=5000
        )
        await authed_page.click('[data-approval-id="ap-deny"] [data-action="deny"]')

        await wait_for(
            lambda: bool(capture["approvals"]),
            msg="No denial response captured",
        )
        resp = capture["approvals"][0]
        assert resp["approval_id"] == "ap-deny"
        assert resp["approved"] is False

    async def test_approve_shows_resolved_state(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-resolved"),
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-resolved"]', timeout=5000
        )
        await authed_page.click(
            '[data-approval-id="ap-resolved"] [data-action="approve"]'
        )

        # Card should briefly show "✓ Approved" before auto-dismissing
        resolved_sel = '[data-approval-id="ap-resolved"] .approval-resolved'
        await authed_page.wait_for_selector(resolved_sel, timeout=5000)
        resolved_text = await authed_page.text_content(resolved_sel)
        assert "Approved" in (resolved_text or "")

    async def test_no_duplicate_card_on_pending_state(
        self, authed_page: Page, test_server: SimpleNamespace
    ) -> None:
        req_id = "ap-dedup"
        payload = _approval_payload(req_id)
        # First injection creates the card
        await inject(
            test_server.ws_handler, test_server.chat_ids, "approval_request", payload
        )
        await authed_page.wait_for_selector(
            f'[data-approval-id="{req_id}"]', timeout=5000
        )

        # pending_state re-sends the same approval (simulates reconnect)
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "pending_state",
            {
                "approvals": [payload],
                "question": None,
                "plan_review": None,
            },
        )
        await asyncio.sleep(0.4)

        # Should still only be ONE card with this request_id
        cards = authed_page.locator(f'[data-approval-id="{req_id}"]')
        assert await cards.count() == 1

    async def test_deny_shows_denied_resolved_state(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-deny-resolved"),
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-deny-resolved"]', timeout=5000
        )
        await authed_page.click(
            '[data-approval-id="ap-deny-resolved"] [data-action="deny"]'
        )

        resolved_sel = '[data-approval-id="ap-deny-resolved"] .approval-resolved'
        await authed_page.wait_for_selector(resolved_sel, timeout=5000)
        resolved_text = await authed_page.text_content(resolved_sel)
        assert "Denied" in (resolved_text or "")

    async def test_approval_card_auto_dismisses_after_resolve(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-dismiss"),
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-dismiss"]', timeout=5000
        )
        await authed_page.click(
            '[data-approval-id="ap-dismiss"] [data-action="approve"]'
        )

        # Card should auto-dismiss after ~1.5s resolve display + animation
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-dismiss"]', state="detached", timeout=5000
        )

    async def test_server_side_approval_resolved(
        self, authed_page: Page, test_server: SimpleNamespace
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-server"),
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-server"]', timeout=5000
        )

        # Server resolves the approval (e.g., AI auto-approver)
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_resolved",
            {"request_id": "ap-server", "approved": True},
        )

        resolved_sel = '[data-approval-id="ap-server"] .approval-resolved'
        await authed_page.wait_for_selector(resolved_sel, timeout=5000)
        resolved_text = await authed_page.text_content(resolved_sel)
        assert "Approved" in (resolved_text or "")

    async def test_multiple_approvals_render_independently(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            _approval_payload("ap-multi-1"),
        )
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            {"request_id": "ap-multi-2", "tool": "Read", "description": "read file"},
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-multi-1"]', timeout=5000
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-multi-2"]', timeout=5000
        )

        # Approve first, deny second
        await authed_page.click(
            '[data-approval-id="ap-multi-1"] [data-action="approve"]'
        )
        await authed_page.click('[data-approval-id="ap-multi-2"] [data-action="deny"]')

        await wait_for(
            lambda: len(capture["approvals"]) >= 2,
            msg="Expected at least 2 approval responses",
        )
        by_id = {r["approval_id"]: r for r in capture["approvals"]}
        assert by_id["ap-multi-1"]["approved"] is True
        assert by_id["ap-multi-2"]["approved"] is False


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.e2e
class TestApproveAll:
    """The WebUI had Approve/Deny only, so every gated call cost a tap."""

    async def test_button_names_the_scope_it_grants(
        self, authed_page: Page, test_server: SimpleNamespace
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            {
                "request_id": "ap-all-label",
                "tool": "Bash::curl raw.githubusercontent.com",
                "description": "Network access",
            },
        )
        card_sel = '[data-approval-id="ap-all-label"]'
        await authed_page.wait_for_selector(card_sel, timeout=5000)

        label = await authed_page.text_content(f"{card_sel} .btn-approve-all")
        assert label == "Approve all “curl raw.githubusercontent.com”"

    async def test_browsing_grant_is_named_as_a_group(
        self, authed_page: Page, test_server: SimpleNamespace
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            {
                "request_id": "ap-all-browse",
                "tool": "Bash::agent-browser click",
                "description": "Click",
                "approve_all_scope": "agent-browser browsing",
            },
        )
        button = '[data-approval-id="ap-all-browse"] .btn-approve-all'
        await authed_page.wait_for_selector(button, timeout=5000)

        assert await authed_page.text_content(button) == (
            "Approve all agent-browser browsing"
        )
        title = await authed_page.get_attribute(button, "title")
        assert title is not None
        assert "Cookies, auth, storage, connect and installs still ask." in title

    async def test_it_approves_and_enables_auto_approve(
        self, authed_page: Page, test_server: SimpleNamespace, capture: dict
    ) -> None:
        tool = "Bash::curl api.github.com"
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            {"request_id": "ap-all-send", "tool": tool, "description": "Network"},
        )
        await authed_page.wait_for_selector(
            '[data-approval-id="ap-all-send"]', timeout=5000
        )
        await authed_page.click(
            '[data-approval-id="ap-all-send"] [data-action="approve-all"]'
        )

        await wait_for(
            lambda: bool(capture["auto_approvals"]),
            msg="No auto-approve captured",
        )
        assert capture["approvals"][-1] == {
            "approval_id": "ap-all-send",
            "approved": True,
        }
        assert capture["auto_approvals"][-1]["tool_name"] == tool

    async def test_a_toolless_prompt_offers_no_blanket_grant(
        self, authed_page: Page, test_server: SimpleNamespace
    ) -> None:
        await inject(
            test_server.ws_handler,
            test_server.chat_ids,
            "approval_request",
            {"request_id": "ap-all-none", "tool": "", "description": "unknown"},
        )
        card_sel = '[data-approval-id="ap-all-none"]'
        await authed_page.wait_for_selector(card_sel, timeout=5000)
        assert await authed_page.query_selector(f"{card_sel} .btn-approve-all") is None
