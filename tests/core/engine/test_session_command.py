"""Engine tests — /session, the multi-conversation picker for one chat."""

import pytest

from leashd.core.engine import Engine
from leashd.core.session import SessionManager
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector
from tests.core.engine.conftest import FakeAgent


class ChatAwareAgent(FakeAgent):
    """Agent that reports which chats currently hold a live pane."""

    def __init__(self):
        super().__init__()
        self.live: set[str] = set()
        self.cancelled_chats: list[str] = []

    def live_chat_ids(self) -> set[str]:
        return set(self.live)

    async def cancel_chat(self, chat_id: str) -> None:
        self.cancelled_chats.append(chat_id)
        self.live.discard(chat_id)


@pytest.fixture
def agent():
    return ChatAwareAgent()


@pytest.fixture
def connector():
    return MockConnector(chat_sessions=True)


@pytest.fixture
async def store(tmp_path):
    """The real store — sessions and messages share it, as in production."""
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.setup()
    yield store
    await store.teardown()


@pytest.fixture
def engine(config, agent, connector, policy_engine, audit_logger, store):
    return Engine(
        connector=connector,
        agent=agent,
        config=config,
        session_manager=SessionManager(store=store),
        policy_engine=policy_engine,
        audit=audit_logger,
        store=store,
    )


def _last(connector):
    return connector.sent_messages[-1]


def _callback_data(message):
    return [btn.callback_data for row in (message["buttons"] or []) for btn in row]


def _noop_cancel(agent):
    """A runtime whose pane survives the terminate, as a stuck one does."""

    async def _keep_it_live(chat_id: str) -> None:
        agent.cancelled_chats.append(chat_id)

    return _keep_it_live


class TestPicker:
    async def test_a_single_chat_lists_one_conversation(self, engine, connector):
        result = await engine.handle_command("u1", "session", "", "chat1")

        assert result == ""
        assert "Conversations in this chat" in _last(connector)["text"]
        assert _callback_data(_last(connector)) == ["sess:sw:1", "sess:new"]

    async def test_the_foreground_conversation_is_marked(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "", "chat1:s2")

        text = _last(connector)["text"]
        assert "▸ #2" in text
        assert "▸ #1" not in text

    async def test_a_live_pane_shows_as_idle_and_a_running_turn_as_working(
        self, engine, connector, agent
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        agent.live = {"chat1", "chat1:s2"}
        engine.executing_chats.add("chat1:s2")

        await engine.handle_command("u1", "session", "", "chat1")

        text = _last(connector)["text"]
        assert "#1 · " in text
        assert "idle" in text
        assert "working" in text

    async def test_a_client_with_its_own_tabs_gets_a_read_only_roster(
        self, config, agent, policy_engine, audit_logger
    ):
        tabbed = MockConnector(chat_sessions=False)
        engine = Engine(
            connector=tabbed,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await engine.handle_command("u1", "session", "", "web:tab:abc")

        assert "Conversations in this chat" in result
        assert tabbed.sent_messages == []


class TestNew:
    async def test_new_creates_the_next_slot_and_attaches_to_it(
        self, engine, connector
    ):
        result = await engine.handle_command("u1", "session", "new", "chat1")

        assert result == ""
        assert connector.activated_chat_sessions == ["chat1:s2"]
        assert engine.session_manager.get("u1", "chat1:s2") is not None
        assert _last(connector)["chat_id"] == "chat1:s2"
        assert "▸ #2" in _last(connector)["text"]

    @staticmethod
    def _two_directories(engine, tmp_path):
        """A picker only has something to offer with more than one directory."""
        other = tmp_path / "other-project"
        other.mkdir(exist_ok=True)
        engine._dir_names = {**engine._dir_names, "other-project": other}
        return other

    async def test_new_offers_the_directory_picker(self, engine, connector, tmp_path):
        """A second conversation is almost always for a different project."""
        self._two_directories(engine, tmp_path)

        await engine.handle_command("u1", "session", "new", "chat1")

        last = _last(connector)
        assert "Select directory" in last["text"]
        assert last["chat_id"] == "chat1:s2"
        assert all(d.startswith("dir:") for d in _callback_data(last))

    async def test_the_picker_marks_the_directory_it_started_in(
        self, engine, connector, tmp_path
    ):
        self._two_directories(engine, tmp_path)

        await engine.handle_command("u1", "session", "new", "chat1")

        marked = [
            btn.text
            for row in _last(connector)["buttons"]
            for btn in row
            if "✅" in btn.text
        ]
        assert len(marked) == 1

    async def test_the_picker_arrives_after_the_banner(
        self, engine, connector, tmp_path
    ):
        """Reversed, the picker reads as belonging to the conversation left behind."""
        self._two_directories(engine, tmp_path)

        await engine.handle_command("u1", "session", "new", "chat1")

        assert "▸ #2" in connector.sent_messages[-2]["text"]
        assert "Select directory" in _last(connector)["text"]

    async def test_naming_a_directory_up_front_skips_the_picker(
        self, engine, connector, tmp_path
    ):
        self._two_directories(engine, tmp_path)

        await engine.handle_command("u1", "session", "new other-project", "chat1")

        assert "Select directory" not in _last(connector)["text"]
        created = engine.session_manager.get("u1", "chat1:s2")
        assert created.working_directory.endswith("other-project")

    async def test_a_single_approved_directory_has_nothing_to_pick(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")

        assert "Select directory" not in _last(connector)["text"]

    async def test_new_inherits_the_current_working_directory(self, engine, tmp_dir):
        current = engine.session_manager.get("u1", "chat1")
        if current is None:
            current = await engine.session_manager.get_or_create(
                "u1", "chat1", str(tmp_dir)
            )
        current.working_directory = str(tmp_dir)

        await engine.handle_command("u1", "session", "new", "chat1")

        created = engine.session_manager.get("u1", "chat1:s2")
        assert created.working_directory == str(tmp_dir)

    async def test_new_accepts_a_named_directory(self, engine, config):
        name = next(iter(engine._dir_names))

        await engine.handle_command("u1", "session", f"new {name}", "chat1")

        created = engine.session_manager.get("u1", "chat1:s2")
        assert created.working_directory == str(engine._dir_names[name])

    async def test_new_rejects_an_unapproved_directory(self, engine):
        result = await engine.handle_command("u1", "session", "new nope", "chat1")

        assert "Unknown directory" in result
        assert engine.session_manager.get("u1", "chat1:s2") is None

    async def test_new_reuses_the_lowest_freed_slot(self, engine):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "kill 2", "chat1")

        await engine.handle_command("u1", "session", "new", "chat1")

        assert engine.session_manager.get("u1", "chat1:s2").is_active

    async def test_the_chat_fills_up(self, engine):
        for _ in range(8):
            await engine.handle_command("u1", "session", "new", "chat1")

        result = await engine.handle_command("u1", "session", "new", "chat1")

        assert "already has 9 conversations" in result


class TestSwitch:
    async def test_a_bare_index_switches(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.activated_chat_sessions.clear()

        result = await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert result == ""
        assert connector.activated_chat_sessions == ["chat1"]

    async def test_switch_replays_the_last_message_of_the_target(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_the_replay_is_not_cut_down_to_fit_the_banner(
        self, engine, connector
    ):
        """The banner is one message; a long reply folded into it lost its tail."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "y" * 9000, "chat1")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"] == "Echo: " + "y" * 9000

    async def test_a_reply_read_live_is_not_replayed_on_the_way_back(
        self, engine, connector
    ):
        """The reply is still in the chat; switching is navigation, not a re-ask.

        Leaving used to forget that the chat had been handed it, so every
        return posted the same answer again.
        """
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert not any(
            "Echo: how are things" in m["text"] for m in connector.sent_messages
        )
        assert _last(connector)["text"].startswith("▸ #1")

    async def test_a_reply_nothing_wrote_over_is_not_replayed_on_a_second_visit(
        self, engine, connector
    ):
        """Two visits to the same idle conversation posted its answer twice."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "1", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "2", "chat1")
        await engine.handle_command("u1", "session", "1", "chat1:s2")

        replays = [
            m for m in connector.sent_messages if m["text"] == "Echo: how are things"
        ]
        assert replays == []

    async def test_a_reply_a_command_wrote_over_is_replayed_again(
        self, engine, connector
    ):
        """Another conversation is not the only thing that can bury an answer.

        The skip used to fire on nothing but a rival conversation's reply, so
        anything else written into the chat — a command's output, here —
        scrolled the answer away and the return still refused to put it back.
        """
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        assert await engine.handle_command("u1", "status", "", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_a_reply_the_roster_wrote_over_is_replayed_again(
        self, engine, connector
    ):
        """The roster is a message of its own, and it lands on top of the answer."""
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_the_landing_banner_alone_does_not_count_as_burying_it(
        self, engine, connector
    ):
        """Or the skip could never fire — a banner precedes every replay."""
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert not any(
            m["text"] == "Echo: how are things" for m in connector.sent_messages
        )

    async def test_a_reply_another_conversation_wrote_over_is_replayed_again(
        self, engine, connector
    ):
        """The reported bug: coming back landed on a bare banner.

        One chat stream carries every conversation in it, so a reply read on
        screen stops being readable the moment another conversation writes
        under it — the return has to put it back.
        """
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "and over here", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_every_return_replays_while_the_chat_keeps_moving(
        self, engine, connector
    ):
        """Not once per reply — switching back and forth is how this is read."""
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "and over here", "chat1:s2")
        connector.sent_messages.clear()

        for _ in range(3):
            await engine.handle_command("u1", "session", "1", "chat1:s2")
            await engine.handle_command("u1", "session", "2", "chat1")

        texts = [m["text"] for m in connector.sent_messages]
        assert texts.count("Echo: how are things") == 3
        assert texts.count("Echo: and over here") == 3

    async def test_switch_to_a_silent_conversation_shows_the_header_alone(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"].startswith("▸ #1")

    async def test_choosing_the_conversation_already_on_screen_just_dismisses(
        self, engine, connector
    ):
        """Re-rendering the picker made the tap look like nothing happened."""
        await engine.handle_command("u1", "session", "1", "chat1")

        text = _last(connector)["text"]
        assert "Conversations in this chat" not in text
        assert text.startswith("▸ #1")

    async def test_choosing_the_one_on_screen_brings_back_a_buried_reply(
        self, engine, connector
    ):
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "", "chat1")

        await engine.handle_command("u1", "session", "1", "chat1")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_choosing_the_one_on_screen_does_not_repeat_a_reply_in_view(
        self, engine, connector
    ):
        await engine.handle_message("u1", "how are things", "chat1")

        await engine.handle_command("u1", "session", "1", "chat1")

        replays = [
            m for m in connector.sent_messages if m["text"] == "Echo: how are things"
        ]
        assert len(replays) == 1
        assert _last(connector)["text"].startswith("▸ #1")

    async def test_held_prompts_are_released_after_the_landing_banner(
        self, engine, connector
    ):
        """Released first, a held question reads as belonging to the one left."""
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.flushed_chat_sessions.clear()
        before = len(connector.sent_messages)

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        chat_id, sent_by_then = connector.flushed_chat_sessions[-1]
        assert chat_id == "chat1"
        assert sent_by_then > before

    async def test_an_unknown_slot_is_reported(self, engine):
        result = await engine.handle_command("u1", "session", "7", "chat1")

        assert result == "No conversation #7 in this chat."

    async def test_switching_does_not_require_an_idle_agent(self, engine, connector):
        """Reaching a second conversation while the first works is the point."""
        await engine.handle_command("u1", "session", "new", "chat1")
        engine.executing_chats.add("chat1:s2")
        connector.activated_chat_sessions.clear()

        result = await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert result == ""
        assert connector.activated_chat_sessions == ["chat1"]


async def _late_reply(engine, chat_id, content):
    from leashd.core.events import LATE_REPLY, Event

    await engine.event_bus.emit(
        Event(
            name=LATE_REPLY,
            data={
                "chat_id": chat_id,
                "user_id": "u1",
                "session_id": "claude-uuid",
                "content": content,
            },
        )
    )


class TestLateReply:
    """A reply claude finishes with no request waiting, as when a background
    watcher's notification starts a turn of its own (bidlens, 2026-10-02)."""

    async def test_it_is_sent_and_stored(self, engine, connector, store):
        await engine.handle_message("u1", "how are things", "chat1")
        connector.sent_messages.clear()

        await _late_reply(engine, "chat1", "  The backfill finished.  ")

        assert [m["text"] for m in connector.sent_messages] == [
            "The backfill finished."
        ]
        rows = await store.get_messages("u1", "chat1")
        assert rows[-1]["role"] == "assistant"
        assert rows[-1]["content"] == "The backfill finished."
        assert rows[-1]["session_id"] == "claude-uuid"

    async def test_a_return_replays_it_rather_than_the_reply_before_it(
        self, engine, connector
    ):
        """The bug: the late reply was sent but never stored, so coming back
        replayed the older answer above it."""
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await _late_reply(engine, "chat1", "The backfill finished.")
        await engine.handle_message("u1", "and over here", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(connector)["text"] == "The backfill finished."

    async def test_one_read_live_is_not_replayed_on_the_way_back(
        self, engine, connector
    ):
        await engine.handle_message("u1", "how are things", "chat1")
        await _late_reply(engine, "chat1", "The backfill finished.")
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert not any(
            "The backfill finished." in m["text"] for m in connector.sent_messages
        )

    async def test_an_empty_one_sends_and_stores_nothing(
        self, engine, connector, store
    ):
        await _late_reply(engine, "chat1", "  \n ")

        assert connector.sent_messages == []
        assert await store.get_messages("u1", "chat1") == []


class TestBanner:
    """The switch banner is chrome, so it must not outlive the switch."""

    @pytest.fixture
    def connector(self):
        return MockConnector(chat_sessions=True, support_streaming=True)

    async def test_a_second_switch_replaces_the_first_banner(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        first = _last(connector)["message_id"]

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert first in [d["message_id"] for d in connector.deleted_messages]
        assert _last(connector)["text"].startswith("▸ #1")

    async def test_talking_again_clears_the_banner(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        banner = _last(connector)["message_id"]

        await engine.handle_message("u1", "hello", "chat1:s2")

        assert banner in [d["message_id"] for d in connector.deleted_messages]

    async def test_a_later_command_clears_the_banner(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        banner = _last(connector)["message_id"]

        await engine.handle_command("u1", "status", "", "chat1:s2")

        assert banner in [d["message_id"] for d in connector.deleted_messages]

    async def test_dismissing_the_picker_leaves_nothing_behind(self, engine, connector):
        await engine.handle_command("u1", "session", "1", "chat1")

        assert (
            connector.scheduled_cleanups[-1]["message_id"]
            == (_last(connector)["message_id"])
        )


class TestMidStreamSwitch:
    @pytest.fixture
    def streaming(self, engine):
        streaming = MockConnector(support_streaming=True, chat_sessions=True)
        engine.connector = streaming
        return streaming

    async def _backgrounded_turn(self, engine, streaming, text="half an answ"):
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        await responder.on_chunk(text)
        engine.active_responders["chat1"] = responder
        await engine.handle_command("u1", "session", "new", "chat1")
        return responder

    async def test_switching_away_mid_stream_withdraws_the_partial_reply(
        self, engine, streaming
    ):
        """Its remaining output is about to be withheld, so the fragment goes."""
        responder = await self._backgrounded_turn(engine, streaming)

        assert streaming.deleted_messages[0]["chat_id"] == "chat1"
        assert await responder.finalize("half an answer, now complete") is False

    async def test_switching_back_mid_turn_puts_the_running_stream_back_on_screen(
        self, engine, streaming
    ):
        """A turn that kept running must not stay mute once it is on screen."""
        responder = await self._backgrounded_turn(engine, streaming)

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert "half an answ" in _last(streaming)["text"]
        await responder.on_chunk("er, now complete")
        assert "half an answer, now complete" in streaming.edited_messages[-1]["text"]
        assert await responder.finalize("half an answer, now complete") is True

    async def test_switching_back_mid_turn_shows_the_header_alone(
        self, engine, streaming
    ):
        """The live turn renders under the banner; a stale replay above it lies."""
        await self._backgrounded_turn(engine, streaming)

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        banner = streaming.sent_messages[-2]["text"]
        assert banner.startswith("▸ #1")
        assert banner.count("\n") == 0

    async def test_the_resumed_stream_opens_a_fresh_message(self, engine, streaming):
        """The messages it had written were deleted on the way out."""
        responder = await self._backgrounded_turn(engine, streaming)
        withdrawn = {d["message_id"] for d in streaming.deleted_messages}

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert set(responder.all_message_ids).isdisjoint(withdrawn)

    async def test_switching_back_to_an_idle_conversation_still_replays(
        self, engine, streaming
    ):
        """A running turn suppresses the replay; so does having already shown it."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert "Echo: how are things" in _last(streaming)["text"]

    async def test_a_turn_that_has_written_nothing_yet_shows_the_last_reply(
        self, engine, streaming
    ):
        """Running is not the same as visible.

        A turn still thinking has nothing to put back on screen, so treating
        it as the thing that renders under the banner left the conversation
        on the banner alone — for as long as the turn stayed quiet.
        """
        from leashd.core.engine import _StreamingResponder

        await engine.handle_message("u1", "how are things", "chat1")
        engine.active_responders["chat1"] = _StreamingResponder(
            streaming, "chat1", throttle_seconds=0
        )
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "and over here", "chat1:s2")
        streaming.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _last(streaming)["text"] == "Echo: how are things"

    async def test_a_turn_that_ends_while_switching_in_still_shows_its_reply(
        self, engine, streaming
    ):
        """The banner's own round trip is long enough for the turn to finish.

        Deciding on a bare header up front and finding the responder gone by
        the time the resume runs left the chat on a header alone: the finished
        reply had already gone out as a background notice and nothing replaced
        it here.
        """
        from leashd.core.engine import _StreamingResponder

        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        await responder.on_chunk("half an answ")
        engine.active_responders["chat1"] = responder

        original = streaming.send_message_with_id

        async def _finish_during_the_banner(chat_id, text):
            engine.active_responders.pop("chat1", None)
            streaming.send_message_with_id = original
            return await original(chat_id, text)

        streaming.send_message_with_id = _finish_during_the_banner

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert streaming.sent_messages[-2]["text"].startswith("▸ #1")
        assert "Echo: how are things" in _last(streaming)["text"]

    async def test_the_replayed_reply_outlives_the_banner(self, engine, streaming):
        """Folded into the banner it went down with it — the reply is content."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "1", "chat1:s2")
        banner, replay = streaming.sent_messages[-2], streaming.sent_messages[-1]
        assert banner["text"].startswith("▸ #1")
        assert replay["text"] == "Echo: how are things"

        await engine.handle_message("u1", "and now this", "chat1")

        assert banner["message_id"] in [
            d["message_id"] for d in streaming.deleted_messages
        ]
        assert "message_id" not in replay

    async def test_a_reply_the_chat_already_has_is_not_replayed_under_the_banner(
        self, engine, streaming
    ):
        """A turn landing during the switch writes it; a replay would double it."""
        await engine.handle_message("u1", "how are things", "chat1")

        await engine._replay_chat_session_transcript(
            engine.session_manager.get("u1", "chat1")
        )

        replays = [
            m for m in streaming.sent_messages if m["text"] == "Echo: how are things"
        ]
        assert len(replays) == 1

    async def test_a_failed_replay_leaves_the_stream_open(self, engine, streaming):
        """A bad send is not a reason to go mute for the rest of the turn."""
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        await responder.on_chunk("half an answ")
        await responder.suspend()

        async def _refuse(chat_id, text):
            return None

        streaming.send_message_with_id = _refuse
        assert await responder.resume() is False

        streaming.send_message_with_id = type(streaming).send_message_with_id.__get__(
            streaming
        )
        await responder.on_chunk("er, now complete")

        assert "er, now complete" in _last(streaming)["text"]


class TestSwitchingBackAndForth:
    """One leave and return is not the contract — every return is.

    The suspend/resume pair rewrites the responder's message ids and display
    offset on both halves, so a state machine that survives the first round
    trip can still strand the second. These walk out and back repeatedly and
    assert the same thing each time: the conversation is never landed on
    empty-handed.
    """

    @pytest.fixture
    def streaming(self, engine):
        streaming = MockConnector(support_streaming=True, chat_sessions=True)
        engine.connector = streaming
        return streaming

    async def test_a_running_turn_comes_back_on_screen_every_time(
        self, engine, streaming
    ):
        """And whole — each return re-renders everything written so far."""
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        await responder.on_chunk("part1. ")
        engine.active_responders["chat1"] = responder
        await engine.handle_command("u1", "session", "new", "chat1")

        for cycle in (1, 2, 3):
            await responder.on_chunk(f"grew{cycle}. ")
            streaming.sent_messages.clear()

            await engine.handle_command("u1", "session", "1", "chat1:s2")

            shown = " ".join(m["text"] for m in streaming.sent_messages)
            assert "part1." in shown, f"return {cycle} lost the opening"
            assert f"grew{cycle}." in shown, f"return {cycle} is stale"
            await engine.handle_command("u1", "session", "2", "chat1")

        await engine.handle_command("u1", "session", "1", "chat1:s2")
        assert await responder.finalize("part1. grew1. grew2. grew3. done") is True
        assert "grew3. done" in streaming.edited_messages[-1]["text"]

    async def test_the_partial_reply_is_withdrawn_on_every_departure(
        self, engine, streaming
    ):
        """A fragment left behind freezes mid-sentence under the other one."""
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        await responder.on_chunk("half an answ")
        engine.active_responders["chat1"] = responder
        await engine.handle_command("u1", "session", "new", "chat1")

        for cycle in (1, 2, 3):
            await engine.handle_command("u1", "session", "1", "chat1:s2")
            onscreen = set(responder.all_message_ids)
            assert onscreen, f"return {cycle} put nothing on screen"

            await engine.handle_command("u1", "session", "2", "chat1")

            withdrawn = {d["message_id"] for d in streaming.deleted_messages}
            assert onscreen <= withdrawn, f"departure {cycle} left a fragment"

    async def test_a_turn_still_thinking_shows_the_last_reply_every_time(
        self, engine, streaming
    ):
        """Running is not visible — and it stays that way across returns.

        Resume takes the responder out of suspension whether or not it had
        anything to show, so a second return finds it unsuspended and must
        still fall through to the replay rather than trusting ``mid_turn``.
        """
        from leashd.core.engine import _StreamingResponder

        await engine.handle_message("u1", "how are things", "chat1")
        engine.active_responders["chat1"] = _StreamingResponder(
            streaming, "chat1", throttle_seconds=0
        )
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "and over here", "chat1:s2")

        for cycle in (1, 2, 3):
            streaming.sent_messages.clear()
            await engine.handle_command("u1", "session", "1", "chat1:s2")
            assert _last(streaming)["text"] == "Echo: how are things", (
                f"return {cycle} left the silent turn on a bare banner"
            )
            await engine.handle_command("u1", "session", "2", "chat1")

    async def test_a_first_turn_running_only_tools_shows_its_tool_every_time(
        self, engine, streaming
    ):
        """No text and no earlier reply must still not land on a bare banner.

        A brand-new conversation whose first turn has only run tools has
        nothing for ``resume`` to render and nothing for the replay to send,
        so the tool it is running is the only thing left to show.
        """
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        engine.active_responders["chat1"] = responder
        await responder.on_activity(ToolActivity(tool_name="Bash", description="ls"))
        await engine.handle_command("u1", "session", "new", "chat1")
        assert "chat1" not in streaming._activity_message_id

        for cycle in (1, 2, 3):
            streaming.activity_messages.clear()
            await engine.handle_command("u1", "session", "1", "chat1:s2")
            shown = [a for a in streaming.activity_messages if a["chat_id"] == "chat1"]
            assert shown, f"return {cycle} hid the running tool behind a bare banner"
            assert shown[-1]["tool_name"] == "Bash"
            await engine.handle_command("u1", "session", "2", "chat1")
            assert "chat1" not in streaming._activity_message_id

    async def test_the_running_tool_follows_the_replayed_reply(self, engine, streaming):
        """Last reply first, then what the turn is doing now, in that order."""
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder

        await engine.handle_message("u1", "how are things", "chat1")
        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        engine.active_responders["chat1"] = responder
        await responder.on_activity(
            ToolActivity(tool_name="WebFetch", description="example.com")
        )
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "and over here", "chat1:s2")
        streaming.sent_messages.clear()
        sent_before_activity: list[str] = []
        original = streaming.send_activity

        async def _recording(chat_id, tool_name, description, **kwargs):
            sent_before_activity.extend(m["text"] for m in streaming.sent_messages)
            return await original(chat_id, tool_name, description, **kwargs)

        streaming.send_activity = _recording

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert sent_before_activity[-1] == "Echo: how are things"
        assert streaming.activity_messages[-1]["tool_name"] == "WebFetch"

    async def test_a_status_line_is_shown_for_a_turn_with_no_text(
        self, engine, streaming
    ):
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(streaming, "chat1", throttle_seconds=0)
        engine.active_responders["chat1"] = responder
        await responder.on_status("⏳ waiting on a background agent")
        await engine.handle_command("u1", "session", "new", "chat1")
        streaming.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert "waiting on a background agent" in _last(streaming)["text"]


class TestTerminate:
    async def test_confirm_kill_asks_before_destroying_anything(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "confirm-kill 2", "chat1:s2")

        assert _callback_data(_last(connector)) == ["sess:kk:2", "sess:list"]
        assert engine.session_manager.get("u1", "chat1:s2").is_active

    async def test_confirming_warns_when_the_conversation_is_working(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        engine.executing_chats.add("chat1:s2")

        await engine.handle_command("u1", "session", "confirm-kill 2", "chat1")

        assert "working right now" in _last(connector)["text"]

    async def test_kill_stops_the_agent_and_drops_the_slot(
        self, engine, connector, agent
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        agent.live = {"chat1:s2"}

        await engine.handle_command("u1", "session", "kill 2", "chat1")

        assert "chat1:s2" in agent.cancelled_chats
        assert engine.session_manager.get("u1", "chat1:s2").is_active is False

    async def test_killing_the_foreground_with_one_left_jumps_straight_there(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.activated_chat_sessions.clear()

        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")

        assert connector.activated_chat_sessions == ["chat1"]
        assert "Conversations in this chat" not in _last(connector)["text"]

    async def test_killing_the_foreground_with_several_left_offers_the_roster(
        self, engine, connector
    ):
        """Picking one for the user lands them wherever sorting happened to put it."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.activated_chat_sessions.clear()

        result = await engine.handle_command("u1", "session", "kill 3", "chat1:s3")

        assert result == ""
        assert "Conversations in this chat" in _last(connector)["text"]
        assert _callback_data(_last(connector))[0] == "sess:sw:1"

    async def test_the_roster_replaces_the_landing_banner_rather_than_following_it(
        self, engine, connector
    ):
        """Both name the same slot, one line apart."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "kill 3", "chat1:s3")

        assert not any(
            m["text"].startswith("▸ #1") for m in connector.sent_messages[-3:]
        )

    async def test_choosing_the_landing_from_the_roster_shows_its_reply(
        self, engine, connector
    ):
        """The kill lands on #1 without a banner, so tapping #1 in the roster
        is already on screen — and answered with a bare header, leaving the
        reply #1 gave while the chat was away nowhere in the chat."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "kill 3", "chat1:s3")

        await engine.handle_command("u1", "session", "1", "chat1")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_the_landing_reply_is_shown_once_however_often_it_is_chosen(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "kill 3", "chat1:s3")
        connector.sent_messages.clear()

        for _ in range(3):
            await engine.handle_command("u1", "session", "1", "chat1")

        replays = [
            m for m in connector.sent_messages if m["text"] == "Echo: how are things"
        ]
        assert len(replays) == 1

    async def test_killing_the_foreground_with_one_left_shows_its_reply(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")

        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")

        assert _last(connector)["text"] == "Echo: how are things"

    async def test_choosing_the_landing_mid_turn_does_not_replay_under_the_stream(
        self, engine, connector
    ):
        from leashd.core.engine import _StreamingResponder

        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1")
        await engine.handle_command("u1", "session", "kill 3", "chat1:s3")
        engine.active_responders["chat1"] = _StreamingResponder(
            connector, "chat1", throttle_seconds=0
        )

        await engine.handle_command("u1", "session", "1", "chat1")

        assert _last(connector)["text"].startswith("▸ #1")

    async def test_the_roster_after_a_kill_no_longer_lists_it(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "kill 3", "chat1:s3")

        text = _last(connector)["text"]
        assert "#3" not in text
        assert "▸ #1" in text

    async def test_killing_a_background_conversation_leaves_the_screen_alone(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.activated_chat_sessions.clear()

        await engine.handle_command("u1", "session", "kill 2", "chat1:s3")

        assert connector.activated_chat_sessions == []
        assert "Terminated #2" in _last(connector)["text"]

    async def test_the_primary_conversation_is_reset_not_removed(self, engine):
        before = engine.session_manager.get("u1", "chat1")
        if before is None:
            await engine.handle_command("u1", "session", "", "chat1")
            before = engine.session_manager.get("u1", "chat1")
        first_id = before.session_id

        await engine.handle_command("u1", "session", "kill 1", "chat1")

        after = engine.session_manager.get("u1", "chat1")
        assert after.is_active is True
        assert after.session_id != first_id

    async def test_the_roster_offers_no_terminate_on_the_one_it_cannot_remove(
        self, engine, connector
    ):
        """Slot 1 is the chat's own id, so there is no slot to free."""
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "", "chat1")

        assert _callback_data(_last(connector)) == [
            "sess:sw:1",
            "sess:sw:2",
            "sess:k:2",
            "sess:new",
        ]

    async def test_killing_the_primary_says_it_was_cleared_not_terminated(
        self, engine, connector
    ):
        """It reported "Terminated #1" and left #1 on the roster, so the chat
        tapped it again and again."""
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "kill 1", "chat1")

        text = _last(connector)["text"]
        assert "Terminated" not in text
        assert "Cleared #1" in text

    async def test_the_primary_is_still_listed_after_it_is_cleared(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "kill 1", "chat1")

        await engine.handle_command("u1", "session", "", "chat1")

        assert "#1" in _last(connector)["text"]

    async def test_confirming_a_primary_kill_describes_a_reset(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "confirm-kill 1", "chat1")

        text = _last(connector)["text"]
        assert "Clear #1" in text
        assert "stays in the list" in text

    async def test_a_terminated_slot_stops_claiming_to_be_the_foreground(self, engine):
        """A conversation that is gone is not the one its chat is showing."""
        await engine.handle_command("u1", "session", "new", "chat1")
        assert engine.session_manager.get("u1", "chat1:s2").is_foreground is True

        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")

        assert engine.session_manager.get("u1", "chat1:s2").is_foreground is False

    async def test_a_pane_outliving_its_terminate_does_not_restore_the_slot(
        self, engine, connector, agent
    ):
        """The roster reads the session cache, which keeps a terminated
        conversation — so a pane the runtime still reports as live must not
        put its row back and make the slot look unkillable."""
        await engine.handle_command("u1", "session", "new", "chat1")
        agent.live = {"chat1", "chat1:s2"}
        agent.cancel_chat = _noop_cancel(agent)

        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")
        await engine.handle_command("u1", "session", "", "chat1")

        assert "#2" not in _last(connector)["text"]

    async def test_killing_a_gone_conversation_is_reported(self, engine):
        result = await engine.handle_command("u1", "session", "kill 4", "chat1")

        assert result == "That conversation is already gone."


class TestSlotReuse:
    """A freed slot is handed to the next conversation; its history is not."""

    async def test_a_new_conversation_never_replays_the_one_it_replaced(
        self, engine, connector
    ):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1:s2")
        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "new", "chat1")

        assert connector.activated_chat_sessions[-1] == "chat1:s2"
        assert not any(
            "Echo: how are things" in m["text"] for m in connector.sent_messages
        )

    async def test_switching_into_the_replacement_stays_quiet_too(
        self, engine, connector
    ):
        """The slot's dead rows outlive the landing, so every arrival is exposed."""
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "how are things", "chat1:s2")
        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "1", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "2", "chat1")

        assert not any(
            "Echo: how are things" in m["text"] for m in connector.sent_messages
        )

    async def test_the_replacement_still_replays_its_own_reply(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "kill 2", "chat1:s2")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_command("u1", "session", "1", "chat1:s2")
        await engine.handle_message("u1", "what now", "chat1:s2")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "2", "chat1")

        assert _last(connector)["text"] == "Echo: what now"


class TestIsolation:
    async def test_conversations_keep_separate_sessions_and_directories(
        self, engine, tmp_dir
    ):
        await engine.handle_message("u1", "first", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "second", "chat1:s2")

        primary = engine.session_manager.get("u1", "chat1")
        secondary = engine.session_manager.get("u1", "chat1:s2")
        assert primary.session_id != secondary.session_id
        assert primary.message_count == 1
        assert secondary.message_count == 1

    async def test_another_chat_never_sees_these_conversations(self, engine, connector):
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "", "chat9")

        assert _callback_data(_last(connector)) == ["sess:sw:1", "sess:new"]


class TestStatus:
    async def test_status_stays_quiet_in_a_single_conversation_chat(self, engine):
        result = await engine.handle_command("u1", "status", "", "chat1")

        assert "Conversation:" not in result

    async def test_status_names_the_conversation_once_there_are_several(self, engine):
        await engine.handle_command("u1", "session", "new", "chat1")

        result = await engine.handle_command("u1", "status", "", "chat1:s2")

        assert result.startswith("Conversation: #2 of 2")


class TestUsage:
    async def test_an_unknown_action_explains_itself(self, engine):
        result = await engine.handle_command("u1", "session", "frobnicate", "chat1")

        assert "Unknown /session action" in result

    async def test_sessions_is_an_alias(self, engine, connector):
        result = await engine.handle_command("u1", "sessions", "", "chat1")

        assert result == ""
        assert "Conversations in this chat" in _last(connector)["text"]
