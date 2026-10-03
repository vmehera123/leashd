"""Telegram connector — multiplexing one chat stream over several conversations."""

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from leashd.connectors.telegram import TelegramConnector


def _make_mock_app():
    app = AsyncMock()
    app.bot = AsyncMock()
    app.updater = AsyncMock()
    app.add_handler = MagicMock()
    counter = itertools.count(1000)

    async def _sent(**_kwargs):
        return MagicMock(message_id=next(counter))

    app.bot.send_message.side_effect = _sent
    return app


async def _switch_to(connector, chat_id):
    """What the engine does on a switch: attach, land its banner, then release."""
    await connector.activate_chat_session(chat_id)
    await connector.flush_chat_session_prompts(chat_id)


@pytest.fixture
def connector():
    conn = TelegramConnector("fake:token")
    conn._app = _make_mock_app()
    return conn


def _sent_chat_ids(connector):
    return [
        call.kwargs["chat_id"]
        for call in connector._app.bot.send_message.call_args_list
    ]


def _sent_texts(connector):
    return [
        call.kwargs["text"] for call in connector._app.bot.send_message.call_args_list
    ]


class TestOutboundRouting:
    async def test_every_slot_lands_in_the_same_telegram_chat(self, connector):
        await connector.send_message("284184690", "primary")
        connector._router.activate("284184690:s2")
        await connector.send_message("284184690:s2", "second")

        assert _sent_chat_ids(connector) == [284184690, 284184690]

    async def test_slot_ids_are_never_sent_to_telegram_verbatim(self, connector):
        connector._router.activate("284184690:s2")
        await connector.send_typing_indicator("284184690:s2")

        assert connector._app.bot.send_chat_action.await_args.kwargs["chat_id"] == (
            284184690
        )


class TestForegroundGating:
    async def test_foreground_output_is_streamed_in_full(self, connector):
        msg_id = await connector.send_message_with_id("284184690", "streaming")

        assert msg_id is not None
        assert _sent_texts(connector) == ["streaming"]

    async def test_background_streaming_is_withheld(self, connector):
        connector._router.activate("284184690:s2")

        msg_id = await connector.send_message_with_id("284184690", "streaming")

        assert msg_id is None
        connector._app.bot.send_message.assert_not_awaited()

    async def test_background_activity_and_typing_are_withheld(self, connector):
        connector._router.activate("284184690:s2")

        activity = await connector.send_activity("284184690", "Bash", "pytest")
        await connector.send_typing_indicator("284184690")

        assert activity is None
        connector._app.bot.send_message.assert_not_awaited()
        connector._app.bot.send_chat_action.assert_not_awaited()

    async def test_background_edits_are_withheld(self, connector):
        connector._router.activate("284184690:s2")

        await connector.edit_message("284184690", "42", "revised")

        connector._app.bot.edit_message_text.assert_not_awaited()

    async def test_background_reply_arrives_as_a_tagged_notice(self, connector):
        connector._router.activate("284184690:s2")

        await connector.send_message("284184690", "the long answer from slot one")

        texts = _sent_texts(connector)
        assert len(texts) == 1
        assert "#1" in texts[0]
        assert "the long answer from slot one" in texts[0]
        markup = connector._app.bot.send_message.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == "sess:sw:1"

    async def test_a_long_background_reply_is_previewed_not_pasted(self, connector):
        connector._router.activate("284184690:s2")

        await connector.send_message("284184690", "x" * 5000)

        text = _sent_texts(connector)[0]
        assert len(text) < 1000
        assert "…" in text


class TestBlockingPromptsWaitForTheirSlot:
    """A prompt is held until the chat is showing the conversation that raised it.

    Rendering it straight away puts an approval or a question under whichever
    conversation happens to be on screen, where it reads as belonging to that
    one — and by the time the user switches to the conversation that is
    actually blocked, it has scrolled out of reach and cannot be found there.
    """

    async def test_a_background_approval_is_announced_not_rendered(self, connector):
        connector._router.activate("284184690")

        await connector.request_approval(
            "284184690:s2", "approval-1", "Run: rm build/", tool_name="Bash"
        )

        text = _sent_texts(connector)[0]
        assert "#2" in text
        assert "Run: rm build/" not in text
        markup = connector._app.bot.send_message.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == "sess:sw:2"

    async def test_switching_in_renders_the_held_approval(self, connector):
        connector._router.activate("284184690")
        await connector.request_approval(
            "284184690:s2", "approval-1", "Run: rm build/", tool_name="Bash"
        )

        await _switch_to(connector, "284184690:s2")

        texts = _sent_texts(connector)
        assert any("Run: rm build/" in t for t in texts)
        assert connector._deferred == {}

    async def test_a_foreground_approval_renders_immediately(self, connector):
        await connector.request_approval(
            "284184690", "approval-1", "Run: rm build/", tool_name="Bash"
        )

        assert "Run: rm build/" in _sent_texts(connector)[0]

    async def test_a_background_question_is_held_until_switched_to(self, connector):
        connector._router.activate("284184690")

        await connector.send_question(
            "284184690:s3",
            "interaction-1",
            "Which database?",
            "Storage",
            [{"label": "Postgres"}],
        )

        assert "Which database?" not in _sent_texts(connector)[0]

        await _switch_to(connector, "284184690:s3")

        assert any("Which database?" in t for t in _sent_texts(connector))

    async def test_a_backgrounded_primary_is_held_too(self, connector):
        """Slot 1 is a background conversation like any other once away from it."""
        connector._router.activate("284184690:s2")

        await connector.request_approval(
            "284184690", "approval-1", "Run: rm build/", tool_name="Bash"
        )

        assert "Run: rm build/" not in _sent_texts(connector)[0]
        assert "#1" in _sent_texts(connector)[0]

    async def test_a_background_plan_review_never_pastes_the_plan(self, connector):
        connector._router.activate("284184690")

        await connector.send_plan_review(
            "284184690:s2", "interaction-1", "# Plan\nStep one\nStep two"
        )

        assert not any("Step one" in t for t in _sent_texts(connector))

        await _switch_to(connector, "284184690:s2")

        assert any("Step one" in t for t in _sent_texts(connector))

    async def test_several_held_prompts_share_one_notice(self, connector):
        connector._router.activate("284184690")

        await connector.request_approval(
            "284184690:s2", "approval-1", "Run: pytest", tool_name="Bash"
        )
        await connector.request_approval(
            "284184690:s2", "approval-2", "Run: ruff", tool_name="Bash"
        )

        assert len(_sent_texts(connector)) == 1
        assert connector._app.bot.edit_message_text.await_count == 1
        assert len(connector._deferred["284184690:s2"]) == 2

    async def test_a_prompt_that_settles_while_held_is_never_shown(self, connector):
        connector._router.activate("284184690")
        await connector.request_approval(
            "284184690:s2", "approval-1", "Run: rm build/", tool_name="Bash"
        )

        connector.discard_prompt("approval-1")
        await _switch_to(connector, "284184690:s2")

        assert not any("Run: rm build/" in t for t in _sent_texts(connector))

    async def test_a_background_interrupt_prompt_is_held_too(self, connector):
        connector._router.activate("284184690")

        msg_id = await connector.send_interrupt_prompt(
            "284184690:s2", "interrupt-1", "run the tests again"
        )

        assert msg_id is None
        assert "run the tests again" not in _sent_texts(connector)[0]

        await _switch_to(connector, "284184690:s2")

        assert any("run the tests again" in t for t in _sent_texts(connector))

    async def test_held_prompts_render_in_the_order_they_were_raised(self, connector):
        connector._router.activate("284184690")
        await connector.request_approval(
            "284184690:s2", "approval-1", "Run: first", tool_name="Bash"
        )
        await connector.request_approval(
            "284184690:s2", "approval-2", "Run: second", tool_name="Bash"
        )

        await _switch_to(connector, "284184690:s2")

        rendered = [
            t.rsplit("\n\n", 1)[-1] for t in _sent_texts(connector) if "Run: " in t
        ]
        assert rendered == ["Run: first", "Run: second"]


class TestLeavingTakesAPromptBackDown:
    """A prompt the user saw stops being readable once the chat moves off it.

    Holding back only the prompts raised *while* a conversation is off screen
    leaves the mirror case in the chat: an approval rendered under #2, still
    unanswered when the user switches to #1, sits there answerable under a
    conversation that never asked it.
    """

    async def test_an_unanswered_approval_is_withdrawn_on_leaving(self, connector):
        await connector.request_approval(
            "284184690", "approval-1", "Run: rm build/", tool_name="Bash"
        )
        sent_id = connector._app.bot.send_message.await_args.kwargs["chat_id"]
        assert sent_id == 284184690

        await connector.activate_chat_session("284184690:s2")

        deleted = [
            call.kwargs["message_id"]
            for call in connector._app.bot.delete_message.call_args_list
        ]
        assert deleted == [1000]
        assert [p.prompt_id for p in connector._deferred["284184690"]] == ["approval-1"]

    async def test_the_withdrawn_prompt_comes_back_when_it_does(self, connector):
        await connector.request_approval(
            "284184690", "approval-1", "Run: rm build/", tool_name="Bash"
        )
        await _switch_to(connector, "284184690:s2")
        assert not any("Run: rm build/" in t for t in _sent_texts(connector)[1:])

        await _switch_to(connector, "284184690")

        assert sum("Run: rm build/" in t for t in _sent_texts(connector)) == 2
        assert connector._onscreen["284184690"][0].prompt_id == "approval-1"

    async def test_leaving_announces_what_is_still_waiting(self, connector):
        await connector.send_question(
            "284184690",
            "interaction-1",
            "Which database?",
            "Storage",
            [{"label": "PG"}],
        )

        await connector.activate_chat_session("284184690:s2")

        assert "🔔 #1 is waiting on you" in _sent_texts(connector)[-1]

    async def test_the_waiting_notice_names_a_titled_conversation(self, connector):
        connector.set_session_title_resolver({"284184690": "Schema migration"}.get)
        await connector.send_question(
            "284184690",
            "interaction-1",
            "Which database?",
            "Storage",
            [{"label": "PG"}],
        )

        await connector.activate_chat_session("284184690:s2")

        assert (
            "🔔 #1 · Schema migration is waiting on you" in (_sent_texts(connector)[-1])
        )

    async def test_a_plan_review_is_withdrawn_whole(self, connector):
        await connector.send_plan_review(
            "284184690", "interaction-1", "# Plan\nStep one"
        )

        await connector.activate_chat_session("284184690:s2")

        deleted = {
            call.kwargs["message_id"]
            for call in connector._app.bot.delete_message.call_args_list
        }
        assert {1000, 1001} <= deleted
        assert not any("Step one" in t for t in _sent_texts(connector)[2:])

    async def test_an_answered_prompt_is_not_resurrected_by_leaving(self, connector):
        await connector.request_approval(
            "284184690", "approval-1", "Run: rm build/", tool_name="Bash"
        )
        connector.discard_prompt("approval-1")

        await _switch_to(connector, "284184690:s2")
        await _switch_to(connector, "284184690")

        assert sum("Run: rm build/" in t for t in _sent_texts(connector)) == 1
        assert connector._deferred == {}

    async def test_a_prompt_raised_off_screen_queues_behind_a_withdrawn_one(
        self, connector
    ):
        await connector.request_approval(
            "284184690", "approval-1", "Run: first", tool_name="Bash"
        )
        await connector.activate_chat_session("284184690:s2")
        await connector.request_approval(
            "284184690", "approval-2", "Run: second", tool_name="Bash"
        )

        await _switch_to(connector, "284184690")

        rendered = [
            t.rsplit("\n\n", 1)[-1] for t in _sent_texts(connector) if "Run: " in t
        ]
        assert rendered == ["Run: first", "Run: first", "Run: second"]

    async def test_a_reissued_prompt_is_cleaned_up_when_it_settles(self, connector):
        """Its owner remembers the first message id, so the connector owns the
        replacement — otherwise a timeout leaves dead buttons in the chat."""
        await connector.request_approval(
            "284184690", "approval-1", "Run: rm build/", tool_name="Bash"
        )
        await _switch_to(connector, "284184690:s2")
        await _switch_to(connector, "284184690")
        reissued = connector._onscreen["284184690"][0].message_ids

        connector.discard_prompt("approval-1")
        for task in list(connector._cleanup_tasks):
            task.cancel()

        assert reissued
        assert reissued != ["1000"]
        assert connector._onscreen == {}

    async def test_the_conversation_switched_into_keeps_its_own_prompts(
        self, connector
    ):
        connector._router.activate("284184690:s2")
        await connector.request_approval(
            "284184690:s2", "approval-2", "Run: mine", tool_name="Bash"
        )

        await connector.activate_chat_session("284184690:s2")

        assert connector._app.bot.delete_message.await_count == 0
        assert connector._deferred == {}


class TestPromptOwnership:
    async def test_an_approval_remembers_the_slot_that_raised_it(self, connector):
        await connector.request_approval(
            "284184690:s2", "approval-1", "Run: pytest", tool_name="Bash"
        )

        assert connector._prompt_chat("approval-1", "284184690") == "284184690:s2"

    async def test_an_unknown_prompt_falls_back_to_the_chat(self, connector):
        assert connector._prompt_chat("nope", "284184690") == "284184690"

    async def test_a_question_remembers_its_slot(self, connector):
        await connector.send_question(
            "284184690:s2", "interaction-1", "Which?", "", [{"label": "A"}]
        )

        assert connector._prompt_chat("interaction-1", "284184690") == "284184690:s2"


class TestActivation:
    def test_the_connector_advertises_chat_sessions(self, connector):
        assert connector.supports_chat_sessions("284184690") is True

    async def test_activate_moves_the_foreground(self, connector):
        await _switch_to(connector, "284184690:s2")

        assert connector._router.inbound("284184690") == "284184690:s2"
