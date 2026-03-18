"""Tests for the WebAgentPlugin."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from leashd.core.events import COMMAND_WEB, Event, EventBus
from leashd.core.session import Session
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    ALL_BROWSER_TOOLS,
)
from leashd.plugins.builtin.web_agent import (
    _LINKEDIN_TASK_TEMPLATE,
    BUILTIN_RECIPES,
    LINKEDIN_COMMENTING,
    WEB_STARTED,
    WebAgentPlugin,
    WebConfig,
    _build_linkedin_recipe,
    _build_web_prompt,
    _read_web_session_context,
    build_web_instruction,
    parse_web_args,
)
from leashd.plugins.builtin.workflow import (
    Playbook,
    PlaybookPhase,
    PlaybookStep,
)


@pytest.fixture
def plugin():
    return WebAgentPlugin()


@pytest.fixture
async def initialized_plugin(plugin, config, event_bus):
    ctx = PluginContext(event_bus=event_bus, config=config)
    await plugin.initialize(ctx)
    return plugin


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="test-session",
        user_id="user1",
        chat_id="chat1",
        working_directory=str(tmp_path),
    )


@pytest.fixture
def gatekeeper():
    mock = MagicMock()
    mock.enable_tool_auto_approve = MagicMock()
    return mock


class TestWebRecipe:
    def test_frozen(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="frozen"):
            LINKEDIN_COMMENTING.name = "other"  # type: ignore[misc]

    def test_linkedin_recipe_fields(self):
        assert LINKEDIN_COMMENTING.name == "linkedin_comment"
        assert LINKEDIN_COMMENTING.platform == "LinkedIn"
        assert LINKEDIN_COMMENTING.base_url == "https://www.linkedin.com"
        assert "AskUserQuestion" in LINKEDIN_COMMENTING.content_review_instruction

    def test_linkedin_task_instruction_has_scan_step(self):
        assert "SCAN" in LINKEDIN_COMMENTING.task_instruction

    def test_linkedin_task_instruction_has_stop_step(self):
        assert "STOP" in LINKEDIN_COMMENTING.task_instruction

    def test_linkedin_task_instruction_has_efficiency_guidance(self):
        assert "EFFICIENCY" in LINKEDIN_COMMENTING.task_instruction
        assert "max 3 snapshots" in LINKEDIN_COMMENTING.task_instruction

    def test_builtin_recipes_contains_linkedin(self):
        assert "linkedin_comment" in BUILTIN_RECIPES
        assert BUILTIN_RECIPES["linkedin_comment"] is LINKEDIN_COMMENTING


class TestWebConfig:
    def test_defaults(self):
        c = WebConfig()
        assert c.recipe_name is None
        assert c.topic is None
        assert c.url is None
        assert c.description == ""

    def test_frozen(self):
        from pydantic import ValidationError

        c = WebConfig(topic="AI")
        with pytest.raises(ValidationError, match="frozen"):
            c.topic = "other"  # type: ignore[misc]

    def test_model_dump(self):
        c = WebConfig(recipe_name="linkedin_comment", topic="AI safety")
        d = c.model_dump()
        assert d["recipe_name"] == "linkedin_comment"
        assert d["topic"] == "AI safety"
        assert d["url"] is None


class TestParseWebArgs:
    def test_empty_args(self):
        c = parse_web_args("")
        assert c == WebConfig()

    def test_whitespace_only(self):
        c = parse_web_args("   ")
        assert c == WebConfig()

    def test_recipe_name(self):
        c = parse_web_args("linkedin_comment")
        assert c.recipe_name == "linkedin_comment"
        assert c.description == ""

    def test_recipe_with_topic(self):
        c = parse_web_args('linkedin_comment --topic "AI safety"')
        assert c.recipe_name == "linkedin_comment"
        assert c.topic == "AI safety"

    def test_topic_short_flag(self):
        c = parse_web_args("linkedin_comment -t AI")
        assert c.recipe_name == "linkedin_comment"
        assert c.topic == "AI"

    def test_topic_unquoted_multiword(self):
        c = parse_web_args("linkedin_comment --topic latest ai news")
        assert c.recipe_name == "linkedin_comment"
        assert c.topic == "latest ai news"

    def test_topic_multiword_before_other_flags(self):
        c = parse_web_args("linkedin_comment --topic latest ai news --fresh")
        assert c.recipe_name == "linkedin_comment"
        assert c.topic == "latest ai news"
        assert c.fresh is True

    def test_url_long_flag(self):
        c = parse_web_args("--url https://example.com check things")
        assert c.url == "https://example.com"
        assert c.description == "check things"

    def test_url_short_flag(self):
        c = parse_web_args("-u https://example.com")
        assert c.url == "https://example.com"

    def test_free_form_description(self):
        c = parse_web_args("browse twitter and find AI posts")
        assert c.recipe_name is None
        assert c.description == "browse twitter and find AI posts"

    def test_unknown_first_token_not_recipe(self):
        c = parse_web_args("unknown_recipe --topic AI")
        assert c.recipe_name is None
        assert c.topic == "AI"
        assert c.description == "unknown_recipe"

    def test_malformed_quotes_fallback(self):
        c = parse_web_args("browse 'unclosed quote")
        assert c.description == "browse 'unclosed quote"

    def test_recipe_with_all_flags(self):
        c = parse_web_args(
            'linkedin_comment --topic "machine learning" --url https://linkedin.com/feed'
        )
        assert c.recipe_name == "linkedin_comment"
        assert c.topic == "machine learning"
        assert c.url == "https://linkedin.com/feed"

    def test_topic_without_value_treated_as_description(self):
        c = parse_web_args("--topic")
        assert c.topic is None
        assert c.description == "--topic"

    def test_fresh_flag(self):
        c = parse_web_args("--fresh linkedin_comment")
        assert c.fresh is True

    def test_fresh_with_recipe_and_topic(self):
        c = parse_web_args('linkedin_comment --fresh --topic "AI safety"')
        assert c.fresh is True
        assert c.recipe_name == "linkedin_comment"
        assert c.topic == "AI safety"

    def test_default_not_fresh(self):
        c = parse_web_args("linkedin_comment")
        assert c.fresh is False

    def test_em_dash_normalized(self):
        c = parse_web_args("linkedin_comment \u2014topic ai")
        assert c.topic == "ai"
        assert c.recipe_name == "linkedin_comment"

    def test_en_dash_normalized(self):
        c = parse_web_args("linkedin_comment \u2013topic ai")
        assert c.topic == "ai"
        assert c.recipe_name == "linkedin_comment"

    def test_em_dash_fresh_flag(self):
        c = parse_web_args("\u2014fresh linkedin_comment")
        assert c.fresh is True


class TestBuildWebInstruction:
    def test_with_recipe(self):
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert "WEB MODE" in instruction
        assert "LinkedIn" in instruction
        assert "AUTHENTICATION" in instruction
        assert "CONTENT REVIEW RULE" in instruction
        assert "CONTEXT PERSISTENCE" in instruction
        assert "RULES" in instruction

    def test_without_recipe(self):
        config = WebConfig(description="browse example.com")
        instruction = build_web_instruction(config, None)
        assert "WEB MODE" in instruction
        assert "browse example.com" in instruction
        assert "CONTENT REVIEW RULE" in instruction
        assert "AskUserQuestion" in instruction

    def test_topic_injected_into_recipe(self):
        config = WebConfig(recipe_name="linkedin_comment", topic="AI safety")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert '"AI safety"' in instruction

    def test_content_review_always_present(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "CONTENT REVIEW RULE (MANDATORY)" in instruction
        assert "MUST NEVER post" in instruction

    def test_generic_auth_without_recipe(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "log in manually" in instruction

    def test_recipe_auth_with_recipe(self):
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert "Navigate to LinkedIn" in instruction

    def test_empty_description_fallback(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "follow the user's instructions" in instruction

    def test_snapshot_rules_updated(self):
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert "verify: true" in instruction
        assert "Do NOT snapshot between sequential actions" in instruction

    def test_context_persistence_section_fresh(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "web-checkpoint.json" in instruction
        assert "web-session.md" in instruction
        assert "fresh session" in instruction

    def test_context_persistence_section_resume(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None, resume=True)
        assert "web-checkpoint.json" in instruction
        assert "web-session.md" in instruction
        assert "resume from recorded progress" in instruction.lower()
        assert "fresh session" not in instruction


class TestBuildWebPrompt:
    def test_with_recipe_and_topic(self):
        config = WebConfig(recipe_name="linkedin_comment", topic="AI safety")
        prompt = _build_web_prompt(config, LINKEDIN_COMMENTING)
        assert "LinkedIn" in prompt
        assert "AI safety" in prompt

    def test_with_recipe_no_topic(self):
        config = WebConfig(recipe_name="linkedin_comment")
        prompt = _build_web_prompt(config, LINKEDIN_COMMENTING)
        assert "LinkedIn" in prompt
        assert "complete the task" in prompt

    def test_without_recipe_description(self):
        config = WebConfig(description="check the news on example.com")
        prompt = _build_web_prompt(config, None)
        assert "check the news on example.com" in prompt

    def test_url_appended(self):
        config = WebConfig(url="https://example.com")
        prompt = _build_web_prompt(config, None)
        assert "https://example.com" in prompt

    def test_session_context_included(self):
        config = WebConfig(description="continue browsing")
        prompt = _build_web_prompt(
            config, None, session_context="Platform: LinkedIn\nStatus: browsing"
        )
        assert "PREVIOUS WEB SESSION CONTEXT" in prompt
        assert "Platform: LinkedIn" in prompt
        assert "Resume from this state" in prompt

    def test_resume_instruction_always_present(self):
        config = WebConfig()
        prompt = _build_web_prompt(config, None)
        assert "web-session.md" in prompt

    def test_recipe_with_url(self):
        config = WebConfig(
            recipe_name="linkedin_comment", topic="AI", url="https://linkedin.com/feed"
        )
        prompt = _build_web_prompt(config, LINKEDIN_COMMENTING)
        assert "LinkedIn" in prompt
        assert "https://linkedin.com/feed" in prompt
        assert "Start at:" in prompt

    def test_description_with_url(self):
        config = WebConfig(description="check the news", url="https://example.com/news")
        prompt = _build_web_prompt(config, None)
        assert "check the news" in prompt
        assert "https://example.com/news" in prompt
        assert "Navigate to:" in prompt

    def test_no_session_context_no_checkpoint_json(self):
        config = WebConfig(description="browse")
        prompt = _build_web_prompt(config, None)
        assert "PREVIOUS WEB SESSION" not in prompt
        assert "browse" in prompt

    def test_checkpoint_json_with_recipe_and_url(self):
        config = WebConfig(
            recipe_name="linkedin_comment", topic="AI", url="https://x.com"
        )
        prompt = _build_web_prompt(
            config, LINKEDIN_COMMENTING, checkpoint_json='{"session_id": "x"}'
        )
        assert "PREVIOUS WEB SESSION STATE" in prompt
        assert "Start at: https://x.com" in prompt


class TestReadWebSessionContext:
    def test_missing_file(self, tmp_path):
        assert _read_web_session_context(str(tmp_path)) is None

    def test_empty_file(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-session.md").write_text("")
        assert _read_web_session_context(str(tmp_path)) is None

    def test_whitespace_only_file(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-session.md").write_text("   \n  \n")
        assert _read_web_session_context(str(tmp_path)) is None

    def test_reads_content(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-session.md").write_text(
            "Platform: LinkedIn\nStatus: browsing"
        )
        result = _read_web_session_context(str(tmp_path))
        assert result == "Platform: LinkedIn\nStatus: browsing"

    def test_truncates_long_content(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        long_content = "x" * 10000
        (tmp_path / ".leashd" / "web-session.md").write_text(long_content)
        result = _read_web_session_context(str(tmp_path))
        assert result is not None
        assert len(result) == 4000

    def test_oserror_on_legacy_file_returns_none(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-session.md").write_text("some content")
        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            result = _read_web_session_context(str(tmp_path))
        assert result is None

    def test_checkpoint_markdown_truncated_to_max_chars(self, tmp_path):
        from leashd.plugins.builtin.web_checkpoint import WebCheckpoint, save_checkpoint

        cp = WebCheckpoint(
            session_id="trunc-1",
            platform="LinkedIn",
            progress_summary="x" * 5000,
            created_at="2026-03-16T10:00:00Z",
            updated_at="2026-03-16T10:05:00Z",
        )
        save_checkpoint(str(tmp_path), cp)
        result = _read_web_session_context(str(tmp_path))
        assert result is not None
        assert len(result) <= 4000


class TestWebAgentPlugin:
    async def test_plugin_meta(self, plugin):
        assert plugin.meta.name == "web_agent"
        assert plugin.meta.version == "0.1.0"

    async def test_plugin_lifecycle(self, plugin):
        await plugin.start()
        await plugin.stop()

    async def test_plugin_sets_mode_and_instruction(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "linkedin_comment --topic AI",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.mode == "web"
        assert "WEB MODE" in session.mode_instruction
        assert "CONTENT REVIEW RULE" in session.mode_instruction

    async def test_plugin_auto_approves_all_browser_tools(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        for tool in ALL_BROWSER_TOOLS:
            gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", tool)

    async def test_plugin_auto_approves_write_edit(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", "Write")

    async def test_plugin_auto_approves_skill_with_linkedin_recipe(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        """Bundled linkedin_comment playbook has no inline_guidance, so Skill
        should be auto-approved for skill invocation."""
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        approved_tools = {
            call.args[1] for call in gatekeeper.enable_tool_auto_approve.call_args_list
        }
        assert "Skill" in approved_tools
        gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", "Edit")

    async def test_plugin_builds_prompt_with_recipe(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "machine learning"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "LinkedIn" in event.data["prompt"]
        assert "machine learning" in event.data["prompt"]

    async def test_plugin_builds_prompt_freeform(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "browse twitter for AI posts",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "browse twitter for AI posts" in event.data["prompt"]

    async def test_plugin_emits_web_started(self, plugin, config, session, gatekeeper):
        event_bus = EventBus()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await plugin.initialize(ctx)

        received: list[Event] = []

        async def capture(ev: Event) -> None:
            received.append(ev)

        event_bus.subscribe(WEB_STARTED, capture)

        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "AI"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert len(received) == 1
        assert received[0].data["chat_id"] == "chat1"
        assert received[0].data["recipe"] == "linkedin_comment"
        assert received[0].data["topic"] == "AI"

    async def test_plugin_uses_linkedin_recipe(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "LinkedIn" in session.mode_instruction
        assert "Navigate to LinkedIn" in session.mode_instruction

    async def test_plugin_resumes_from_session_context(
        self, initialized_plugin, event_bus, session, gatekeeper, tmp_path
    ):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "web-session.md").write_text(
            "Platform: LinkedIn\nProgress: 3 posts"
        )

        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test" --resume',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "PREVIOUS WEB SESSION CONTEXT" in event.data["prompt"]
        assert "3 posts" in event.data["prompt"]

    async def test_plugin_no_recipe_generic_instruction(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--url https://example.com check the latest",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.mode == "web"
        assert "log in manually" in session.mode_instruction
        assert "https://example.com" in event.data["prompt"]

    async def test_plugin_sets_browser_fresh_when_fresh_flag(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--fresh linkedin_comment",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.browser_fresh is True

    async def test_plugin_browser_fresh_false_by_default(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.browser_fresh is False

    async def test_non_resume_clears_agent_resume_token(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        session.agent_resume_token = "old-session-123"
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "new topic"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.agent_resume_token is None

    async def test_resume_preserves_agent_resume_token(
        self, initialized_plugin, event_bus, session, gatekeeper, tmp_path
    ):
        session.agent_resume_token = "old-session-123"
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "web-session.md").write_text("Platform: LinkedIn")

        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test" --resume',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.agent_resume_token == "old-session-123"


class TestGatekeeperNegativeAutoApproval:
    async def test_auto_approved_without_inline_guidance(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        """When no playbook (freeform), Skill IS auto-approved."""
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--url https://example.com browse",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        approved_tools = {
            call.args[1] for call in gatekeeper.enable_tool_auto_approve.call_args_list
        }
        expected = (
            ALL_BROWSER_TOOLS | AGENT_BROWSER_AUTO_APPROVE | {"Write", "Edit", "Skill"}
        )
        assert approved_tools == expected
        assert "Read" not in approved_tools

    async def test_auto_approved_with_linkedin_recipe_includes_skill(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        """Bundled linkedin_comment playbook has no inline_guidance, so Skill
        is auto-approved alongside browser tools."""
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        approved_tools = {
            call.args[1] for call in gatekeeper.enable_tool_auto_approve.call_args_list
        }
        expected = (
            ALL_BROWSER_TOOLS | AGENT_BROWSER_AUTO_APPROVE | {"Write", "Edit", "Skill"}
        )
        assert approved_tools == expected
        assert "Read" not in approved_tools

    async def test_plugin_sets_session_browser_backend(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)
        assert session.browser_backend == "playwright"

    async def test_config_reload_updates_backend(self, config, event_bus):
        plugin = WebAgentPlugin()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await plugin.initialize(ctx)
        assert plugin._browser_backend == "playwright"

        from leashd.core.events import CONFIG_RELOADED

        await event_bus.emit(
            Event(
                name=CONFIG_RELOADED,
                data={"browser_backend": "agent-browser"},
            )
        )
        assert plugin._browser_backend == "agent-browser"

    async def test_config_reload_noop_for_same_backend(self, config, event_bus):
        plugin = WebAgentPlugin()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await plugin.initialize(ctx)

        from leashd.core.events import CONFIG_RELOADED

        await event_bus.emit(
            Event(
                name=CONFIG_RELOADED,
                data={"browser_backend": "playwright"},
            )
        )
        assert plugin._browser_backend == "playwright"


class TestRecipeNotFound:
    def test_unknown_recipe_treated_as_description(self):
        c = parse_web_args("nonexistent --topic AI")
        assert c.recipe_name is None
        assert "nonexistent" in c.description

    async def test_plugin_unknown_recipe_falls_back(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "nonexistent",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.mode == "web"
        assert "log in manually" in session.mode_instruction


class TestBuildWebInstructionWithSkills:
    def test_skills_section_present_when_matching(self):
        from leashd.skills import SkillInfo

        mock_skills = [
            SkillInfo(
                name="linkedin-writer",
                description="Professional LinkedIn comment writing style",
                installed_at="2026-03-09",
                source="/tmp/lw.zip",
                tags=["web", "content"],
            )
        ]
        config = WebConfig(recipe_name="linkedin_comment")
        with patch(
            "leashd.plugins.builtin.web_agent.get_skills_by_tag",
            side_effect=lambda tag: mock_skills if tag == "web" else [],
        ):
            instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert "AVAILABLE SKILLS" in instruction
        assert "linkedin-writer" in instruction
        assert "Professional LinkedIn comment" in instruction
        assert "Skill tool" in instruction

    def test_no_skills_section_when_none_installed(self):
        config = WebConfig(recipe_name="linkedin_comment")
        with patch(
            "leashd.plugins.builtin.web_agent.get_skills_by_tag",
            return_value=[],
        ):
            instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert "AVAILABLE SKILLS" not in instruction

    def test_skills_deduped_across_tags(self):
        from leashd.skills import SkillInfo

        skill = SkillInfo(
            name="multi-tag",
            description="Has both web and content tags",
            installed_at="2026-03-09",
            source="/tmp/mt.zip",
            tags=["web", "content"],
        )
        config = WebConfig()
        with patch(
            "leashd.plugins.builtin.web_agent.get_skills_by_tag",
            return_value=[skill],
        ):
            instruction = build_web_instruction(config, None)
        assert instruction.count("multi-tag:") == 1

    def test_skills_section_between_task_and_content_review(self):
        from leashd.skills import SkillInfo

        mock_skills = [
            SkillInfo(
                name="test-skill",
                description="Test",
                installed_at="2026-03-09",
                source="/tmp/ts.zip",
                tags=["web"],
            )
        ]
        config = WebConfig(recipe_name="linkedin_comment")
        with patch(
            "leashd.plugins.builtin.web_agent.get_skills_by_tag",
            side_effect=lambda tag: mock_skills if tag == "web" else [],
        ):
            instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        task_pos = instruction.index("TASK:")
        skills_pos = instruction.index("AVAILABLE SKILLS:")
        review_pos = instruction.index("CONTENT REVIEW RULE")
        assert task_pos < skills_pos < review_pos

    def test_inline_guidance_playbook_suppresses_skills(self):
        from leashd.skills import SkillInfo

        mock_skills = [
            SkillInfo(
                name="linkedin-writer",
                description="Professional LinkedIn comment writing",
                installed_at="2026-03-09",
                source="/tmp/lw.zip",
                tags=["web"],
            )
        ]
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            inline_guidance="Use short comments. Be specific.",
        )
        config = WebConfig(recipe_name="linkedin_comment")
        with patch(
            "leashd.plugins.builtin.web_agent.get_skills_by_tag",
            side_effect=lambda tag: mock_skills if tag == "web" else [],
        ):
            instruction = build_web_instruction(config, LINKEDIN_COMMENTING, playbook)
        assert "AVAILABLE SKILLS" not in instruction
        assert "linkedin-writer" not in instruction

    def test_no_inline_guidance_still_shows_skills(self):
        from leashd.skills import SkillInfo

        mock_skills = [
            SkillInfo(
                name="linkedin-writer",
                description="Professional LinkedIn comment writing",
                installed_at="2026-03-09",
                source="/tmp/lw.zip",
                tags=["web"],
            )
        ]
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
        )
        config = WebConfig(recipe_name="linkedin_comment")
        with patch(
            "leashd.plugins.builtin.web_agent.get_skills_by_tag",
            side_effect=lambda tag: mock_skills if tag == "web" else [],
        ):
            instruction = build_web_instruction(config, LINKEDIN_COMMENTING, playbook)
        assert "AVAILABLE SKILLS" in instruction


class TestBuildWebInstructionWithPlaybook:
    def test_playbook_injected_into_instruction(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="TestPlatform",
            url_patterns={"search": "https://example.com/search?q={topic}"},
            element_patterns={"button": "Submit button"},
            phases=[
                PlaybookPhase(
                    name="navigate",
                    steps=[
                        PlaybookStep(action="navigate", description="Open search page"),
                    ],
                ),
            ],
        )
        config = WebConfig(recipe_name="linkedin_comment", topic="AI")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING, playbook)
        assert "NAVIGATION GUIDE (TestPlatform)" in instruction
        assert "https://example.com/search?q=AI" in instruction
        assert "Submit button" in instruction

    def test_no_playbook_no_navigation_guide(self):
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING, None)
        assert "NAVIGATION GUIDE" not in instruction

    def test_playbook_between_task_and_content_review(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            url_patterns={"url": "https://example.com"},
        )
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING, playbook)
        task_pos = instruction.index("TASK:")
        nav_pos = instruction.index("NAVIGATION GUIDE")
        review_pos = instruction.index("CONTENT REVIEW RULE")
        assert task_pos < nav_pos < review_pos

    def test_backend_passthrough_to_formatter(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="click",
                            description="Click",
                            tool_hint="browser_click",
                        ),
                    ],
                ),
            ],
        )
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(
            config, LINKEDIN_COMMENTING, playbook, browser_backend="agent-browser"
        )
        assert "Tool: agent-browser click" in instruction
        assert "Tool: browser_click" not in instruction


class TestDomExplorationRules:
    def test_dom_prohibition_when_playbook_has_scripts(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="evaluate",
                            description="Run extraction",
                            script="document.title",
                        ),
                    ],
                ),
            ],
        )
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING, playbook)
        assert "Do NOT use browser_evaluate to explore DOM structure" in instruction
        assert "Max 2 retries per script" in instruction

    def test_no_dom_prohibition_without_scripts(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(action="click", description="Click"),
                    ],
                ),
            ],
        )
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING, playbook)
        assert "Do NOT use browser_evaluate to explore DOM structure" not in instruction

    def test_dom_prohibition_uses_agent_browser_tool(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="evaluate",
                            description="Run script",
                            script="document.title",
                        ),
                    ],
                ),
            ],
        )
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, playbook, browser_backend="agent-browser"
        )
        assert "Do NOT use agent-browser eval to explore DOM structure" in instruction
        assert "agent-browser get text" in instruction

    def test_get_text_rule_not_present_for_playwright(self):
        playbook = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="evaluate",
                            description="Run script",
                            script="document.title",
                        ),
                    ],
                ),
            ],
        )
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, playbook, browser_backend="playwright"
        )
        assert "agent-browser get text" not in instruction

    def test_no_dom_prohibition_without_playbook(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "Do NOT use" not in instruction
        assert "Max 2 retries" not in instruction


class TestWebRecipeTaskInstruction:
    def test_linkedin_references_playbook_guide(self):
        assert "COMMENT DRAFTING GUIDE in the playbook" in (
            LINKEDIN_COMMENTING.task_instruction
        )

    def test_linkedin_does_not_reference_skill(self):
        assert "linkedin-comment skill" not in LINKEDIN_COMMENTING.task_instruction

    def test_linkedin_task_instruction_prohibits_js_for_typing(self):
        assert "NEVER use browser_evaluate" in LINKEDIN_COMMENTING.task_instruction


class TestPluginLoadsPlaybook:
    async def test_plugin_loads_bundled_playbook(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "AI safety"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "NAVIGATION GUIDE" in session.mode_instruction
        assert "AI safety" in session.mode_instruction

    async def test_plugin_no_playbook_for_freeform(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "browse twitter for AI posts",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "NAVIGATION GUIDE" not in session.mode_instruction


class TestBuildWebInstructionBackend:
    def test_default_playwright_mentions_mcp(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "browser MCP tools" in instruction
        assert "Playwright MCP" in instruction

    def test_agent_browser_mentions_cli(self):
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, browser_backend="agent-browser"
        )
        assert "agent-browser CLI" in instruction
        assert "agent-browser open" in instruction
        assert "Playwright MCP" not in instruction

    def test_agent_browser_includes_close_instruction(self):
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, browser_backend="agent-browser"
        )
        assert "agent-browser close" in instruction
        assert "stale browser session" in instruction

    def test_agent_browser_close_skipped_on_resume(self):
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, browser_backend="agent-browser", resume=True
        )
        assert "stale browser session" not in instruction

    def test_playwright_no_close_instruction(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None, browser_backend="playwright")
        assert "agent-browser close" not in instruction

    def test_agent_browser_auth_uses_snapshot(self):
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, browser_backend="agent-browser"
        )
        assert "agent-browser snapshot -i" in instruction

    def test_playwright_auth_uses_browser_snapshot(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None, browser_backend="playwright")
        assert "browser_snapshot" in instruction

    def test_agent_browser_rules_use_correct_tools(self):
        config = WebConfig()
        instruction = build_web_instruction(
            config, None, browser_backend="agent-browser"
        )
        assert "agent-browser snapshot -i" in instruction
        assert "agent-browser screenshot" in instruction

    def test_playwright_rules_use_correct_tools(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None, browser_backend="playwright")
        assert "browser_snapshot" in instruction
        assert "browser_take_screenshot" in instruction


class TestResumeFlag:
    def test_parse_resume_flag(self):
        c = parse_web_args("linkedin_comment --resume")
        assert c.resume is True
        assert c.recipe_name == "linkedin_comment"

    def test_default_not_resume(self):
        c = parse_web_args("linkedin_comment")
        assert c.resume is False

    async def test_default_no_session_resume(
        self, initialized_plugin, event_bus, session, gatekeeper, tmp_path
    ):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "web-session.md").write_text(
            "Platform: LinkedIn\nProgress: 3 posts"
        )

        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "PREVIOUS WEB SESSION CONTEXT" not in event.data["prompt"]


class TestTopicValidation:
    async def test_recipe_missing_topic_returns_error(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "linkedin_comment",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert event.data["prompt"] == ""
        assert "error" in event.data
        assert "requires a topic" in event.data["error"]

    async def test_recipe_with_topic_no_error(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "AI safety"',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "error" not in event.data
        assert event.data["prompt"] != ""


class TestRecipeTextWithRegistry:
    def test_task_resolved_for_agent_browser(self):
        recipe = _build_linkedin_recipe("agent-browser")
        assert "agent-browser eval" in recipe.task_instruction
        assert "agent-browser click" in recipe.task_instruction
        assert "agent-browser type" in recipe.task_instruction
        assert "{snap_tool}" not in recipe.task_instruction
        assert "{eval_tool}" not in recipe.task_instruction

    def test_task_resolved_for_playwright(self):
        recipe = _build_linkedin_recipe("playwright")
        assert "browser_evaluate" in recipe.task_instruction
        assert "browser_click" in recipe.task_instruction
        assert "browser_type" in recipe.task_instruction
        assert "{snap_tool}" not in recipe.task_instruction

    def test_auth_resolved_for_agent_browser(self):
        recipe = _build_linkedin_recipe("agent-browser")
        assert "agent-browser snapshot -i" in recipe.auth_instruction
        assert "browser_snapshot" not in recipe.auth_instruction


class TestLinkedInPlaybookCommentBugFix:
    def test_playbook_has_verify_draft_step(self):
        from leashd.plugins.builtin.workflow import load_playbook

        playbook = load_playbook(str(Path(__file__).parent), "linkedin_comment")
        assert playbook is not None
        comment_phase = next(
            (p for p in playbook.phases if p.name == "comment_on_post"), None
        )
        assert comment_phase is not None
        verify_step = next(
            (s for s in comment_phase.steps if s.action == "verify_draft"), None
        )
        assert verify_step is not None
        assert verify_step.verify is True
        assert "approved draft" in (verify_step.notes or "")

    def test_playbook_type_step_has_clear_instruction(self):
        from leashd.plugins.builtin.workflow import load_playbook

        playbook = load_playbook(str(Path(__file__).parent), "linkedin_comment")
        assert playbook is not None
        comment_phase = next(
            (p for p in playbook.phases if p.name == "comment_on_post"), None
        )
        assert comment_phase is not None
        type_step = next((s for s in comment_phase.steps if s.action == "type"), None)
        assert type_step is not None
        notes = type_step.notes or ""
        assert "Select All" in notes or "clear" in notes.lower()

    def test_playbook_verify_draft_before_submit(self):
        from leashd.plugins.builtin.workflow import load_playbook

        playbook = load_playbook(str(Path(__file__).parent), "linkedin_comment")
        assert playbook is not None
        comment_phase = next(
            (p for p in playbook.phases if p.name == "comment_on_post"), None
        )
        assert comment_phase is not None
        actions = [s.action for s in comment_phase.steps]
        verify_idx = actions.index("verify_draft")
        submit_idx = actions.index("submit")
        assert verify_idx < submit_idx

    def test_task_template_has_verification(self):
        assert "verification snapshot" in _LINKEDIN_TASK_TEMPLATE
        assert "clear the editor" in _LINKEDIN_TASK_TEMPLATE.lower() or (
            "Select All" in _LINKEDIN_TASK_TEMPLATE
        )

    def test_submit_step_no_retype_recovery(self):
        from leashd.plugins.builtin.workflow import load_playbook

        playbook = load_playbook(str(Path(__file__).parent), "linkedin_comment")
        assert playbook is not None
        comment_phase = next(
            (p for p in playbook.phases if p.name == "comment_on_post"), None
        )
        assert comment_phase is not None
        submit_step = next(
            (s for s in comment_phase.steps if s.action == "submit"), None
        )
        assert submit_step is not None
        notes = submit_step.notes or ""
        assert "re-type" not in notes.lower()
        assert "verify_draft" in notes


class TestCheckpointIntegration:
    def test_read_session_context_prefers_checkpoint(self, tmp_path):
        from leashd.plugins.builtin.web_checkpoint import (
            WebCheckpoint,
            save_checkpoint,
        )

        cp = WebCheckpoint(
            session_id="cp-1",
            platform="LinkedIn",
            created_at="2026-03-16T10:00:00Z",
            updated_at="2026-03-16T10:05:00Z",
        )
        save_checkpoint(str(tmp_path), cp)
        # Also write legacy markdown
        (tmp_path / ".leashd" / "web-session.md").write_text("legacy content")

        result = _read_web_session_context(str(tmp_path))
        assert result is not None
        assert "LinkedIn" in result
        assert "legacy content" not in result

    def test_read_session_context_falls_back_to_markdown(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-session.md").write_text("Platform: LinkedIn")
        result = _read_web_session_context(str(tmp_path))
        assert result == "Platform: LinkedIn"

    def test_prompt_includes_checkpoint_json_when_available(self):
        config = WebConfig(recipe_name="linkedin_comment", topic="AI")
        checkpoint_json = '{"session_id": "x", "platform": "LinkedIn"}'
        prompt = _build_web_prompt(
            config, LINKEDIN_COMMENTING, checkpoint_json=checkpoint_json
        )
        assert "PREVIOUS WEB SESSION STATE" in prompt
        assert "web-checkpoint.json" in prompt
        assert checkpoint_json in prompt

    def test_prompt_prefers_checkpoint_over_session_context(self):
        config = WebConfig(recipe_name="linkedin_comment", topic="AI")
        prompt = _build_web_prompt(
            config,
            LINKEDIN_COMMENTING,
            session_context="legacy session data",
            checkpoint_json='{"session_id": "x"}',
        )
        assert "PREVIOUS WEB SESSION STATE" in prompt
        assert "legacy session data" not in prompt

    def test_prompt_uses_session_context_without_checkpoint(self):
        config = WebConfig(recipe_name="linkedin_comment", topic="AI")
        prompt = _build_web_prompt(
            config,
            LINKEDIN_COMMENTING,
            session_context="legacy session data",
        )
        assert "PREVIOUS WEB SESSION CONTEXT" in prompt
        assert "legacy session data" in prompt

    def test_fresh_instruction_mentions_checkpoint_json(self):
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING)
        assert "web-checkpoint.json" in instruction

    def test_resume_instruction_mentions_checkpoint_json(self):
        config = WebConfig(recipe_name="linkedin_comment")
        instruction = build_web_instruction(config, LINKEDIN_COMMENTING, resume=True)
        assert "web-checkpoint.json" in instruction
        assert "fall back to" in instruction.lower()

    async def test_resume_with_checkpoint_json_takes_precedence(
        self, initialized_plugin, event_bus, session, gatekeeper, tmp_path
    ):
        from leashd.plugins.builtin.web_checkpoint import (
            WebCheckpoint,
            save_checkpoint,
        )

        cp = WebCheckpoint(
            session_id="cp-resume",
            platform="LinkedIn",
            auth_status="authenticated",
            created_at="2026-03-16T10:00:00Z",
            updated_at="2026-03-16T10:05:00Z",
        )
        save_checkpoint(str(tmp_path), cp)

        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test" --resume',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "PREVIOUS WEB SESSION STATE" in event.data["prompt"]
        assert "cp-resume" in event.data["prompt"]

    async def test_resume_with_only_legacy_markdown_still_works(
        self, initialized_plugin, event_bus, session, gatekeeper, tmp_path
    ):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "web-session.md").write_text("Platform: LinkedIn\nLegacy data")

        event = Event(
            name=COMMAND_WEB,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": 'linkedin_comment --topic "test" --resume',
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "PREVIOUS WEB SESSION CONTEXT" in event.data["prompt"]
        assert "Legacy data" in event.data["prompt"]


class TestCheckpointFieldsInPrompt:
    def test_fresh_instruction_mentions_progress_and_pending(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "progress_summary" in instruction
        assert "pending_work" in instruction

    def test_resume_instruction_mentions_progress_and_pending(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None, resume=True)
        assert "progress_summary" in instruction
        assert "pending_work" in instruction

    def test_checkpoint_instruction_contains_comment_phase(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "comment_phase" in instruction

    def test_checkpoint_has_specific_trigger_points(self):
        config = WebConfig()
        instruction = build_web_instruction(config, None)
        assert "After authentication completes" in instruction
        assert "After typing/filling the comment" in instruction
        assert "After successfully submitting" in instruction

    def test_task_template_has_agent_browser_fill_guidance(self):
        assert "fill" in _LINKEDIN_TASK_TEMPLATE
        assert "contenteditable" in _LINKEDIN_TASK_TEMPLATE

    def test_task_template_has_submit_button_guidance(self):
        assert "NOT the main feed" in _LINKEDIN_TASK_TEMPLATE
        assert "find role button name Post click" in _LINKEDIN_TASK_TEMPLATE
