"""Tests for the workflow playbook module."""

import pytest
import yaml

from leashd.plugins.builtin.workflow import (
    BackendStepOverride,
    Playbook,
    PlaybookPhase,
    PlaybookStep,
    format_playbook_instruction,
    list_playbooks,
    load_playbook,
    playbook_requires_topic,
    resolve_step,
)

# --- PlaybookStep ---


class TestPlaybookStep:
    def test_minimal_step(self):
        step = PlaybookStep(action="navigate", description="Go to URL")
        assert step.action == "navigate"
        assert step.description == "Go to URL"
        assert step.target is None

    def test_full_step(self):
        step = PlaybookStep(
            action="click",
            description="Click button",
            target="submit_button",
            value="Submit",
            expected_state="Form submitted",
            notes="Wait for confirmation",
            fallback="Retry once",
        )
        assert step.target == "submit_button"
        assert step.fallback == "Retry once"

    def test_tool_hint_field(self):
        step = PlaybookStep(
            action="click",
            description="Click submit",
            tool_hint="browser_click",
        )
        assert step.tool_hint == "browser_click"

    def test_verify_defaults_true(self):
        step = PlaybookStep(action="click", description="Click")
        assert step.verify is True

    def test_verify_false(self):
        step = PlaybookStep(action="type", description="Type text", verify=False)
        assert step.verify is False

    def test_script_defaults_none(self):
        step = PlaybookStep(action="click", description="Click")
        assert step.script is None

    def test_script_field(self):
        step = PlaybookStep(
            action="evaluate",
            description="Run script",
            script="document.title",
        )
        assert step.script == "document.title"

    def test_tool_hint_defaults_none(self):
        step = PlaybookStep(action="click", description="Click")
        assert step.tool_hint is None

    def test_frozen(self):
        from pydantic import ValidationError

        step = PlaybookStep(action="click", description="Click")
        with pytest.raises(ValidationError, match="frozen"):
            step.action = "type"  # type: ignore[misc]


# --- PlaybookPhase ---


class TestPlaybookPhase:
    def test_minimal_phase(self):
        phase = PlaybookPhase(name="auth")
        assert phase.name == "auth"
        assert phase.steps == []

    def test_phase_with_steps(self):
        steps = [
            PlaybookStep(action="navigate", description="Go to login"),
            PlaybookStep(action="verify", description="Check login state"),
        ]
        phase = PlaybookPhase(name="auth", description="Authenticate", steps=steps)
        assert len(phase.steps) == 2
        assert phase.description == "Authenticate"


# --- Playbook ---


class TestPlaybook:
    def test_minimal_playbook(self):
        pb = Playbook(name="Test", recipe="test", platform="Web")
        assert pb.name == "Test"
        assert pb.url_patterns == {}
        assert pb.phases == []

    def test_full_playbook(self):
        pb = Playbook(
            name="LinkedIn Commenting",
            recipe="linkedin_comment",
            platform="LinkedIn",
            url_patterns={"search": "https://linkedin.com/search?q={topic}"},
            element_patterns={"comment_button": "Button labeled 'Comment'"},
            phases=[PlaybookPhase(name="auth")],
        )
        assert pb.url_patterns["search"] == "https://linkedin.com/search?q={topic}"
        assert len(pb.element_patterns) == 1
        assert len(pb.phases) == 1

    def test_inline_guidance_defaults_none(self):
        pb = Playbook(name="Test", recipe="test", platform="Web")
        assert pb.inline_guidance is None

    def test_inline_guidance_field(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Web",
            inline_guidance="Use short comments. Be specific.",
        )
        assert pb.inline_guidance == "Use short comments. Be specific."

    def test_frozen(self):
        from pydantic import ValidationError

        pb = Playbook(name="Test", recipe="test", platform="Web")
        with pytest.raises(ValidationError, match="frozen"):
            pb.name = "Other"  # type: ignore[misc]


# --- load_playbook ---


class TestLoadPlaybook:
    def test_loads_bundled_linkedin_playbook(self, tmp_path):
        pb = load_playbook(str(tmp_path), "linkedin_comment")
        assert pb is not None
        assert pb.platform == "LinkedIn"
        assert "search" in pb.url_patterns
        assert len(pb.phases) > 0

    def test_returns_none_for_unknown(self, tmp_path):
        assert load_playbook(str(tmp_path), "nonexistent_recipe") is None

    def test_project_local_overrides_bundled(self, tmp_path):
        workflows_dir = tmp_path / ".leashd" / "workflows"
        workflows_dir.mkdir(parents=True)
        data = {
            "name": "Custom LinkedIn",
            "recipe": "linkedin_comment",
            "platform": "LinkedIn",
            "url_patterns": {"custom": "https://custom.example.com"},
        }
        (workflows_dir / "linkedin_comment.yaml").write_text(yaml.dump(data))

        pb = load_playbook(str(tmp_path), "linkedin_comment")
        assert pb is not None
        assert pb.name == "Custom LinkedIn"
        assert "custom" in pb.url_patterns

    def test_yml_extension_works(self, tmp_path):
        workflows_dir = tmp_path / ".leashd" / "workflows"
        workflows_dir.mkdir(parents=True)
        data = {
            "name": "YML Test",
            "recipe": "yml_test",
            "platform": "Test",
        }
        (workflows_dir / "yml_test.yml").write_text(yaml.dump(data))

        pb = load_playbook(str(tmp_path), "yml_test")
        assert pb is not None
        assert pb.name == "YML Test"

    def test_invalid_yaml_skipped(self, tmp_path):
        workflows_dir = tmp_path / ".leashd" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "bad.yaml").write_text("not: [valid: yaml: {")

        assert load_playbook(str(tmp_path), "bad") is None

    def test_non_dict_yaml_skipped(self, tmp_path):
        workflows_dir = tmp_path / ".leashd" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "listonly.yaml").write_text("- item1\n- item2\n")

        assert load_playbook(str(tmp_path), "listonly") is None


# --- list_playbooks ---


class TestListPlaybooks:
    def test_lists_bundled_playbooks(self, tmp_path):
        playbooks = list_playbooks(str(tmp_path))
        names = [name for name, _ in playbooks]
        assert "linkedin_comment" in names
        sources = dict(playbooks)
        assert sources["linkedin_comment"] == "bundled"

    def test_project_local_listed_first(self, tmp_path):
        workflows_dir = tmp_path / ".leashd" / "workflows"
        workflows_dir.mkdir(parents=True)
        data = {"name": "Custom", "recipe": "custom", "platform": "Test"}
        (workflows_dir / "custom.yaml").write_text(yaml.dump(data))

        playbooks = list_playbooks(str(tmp_path))
        names = [name for name, _ in playbooks]
        assert "custom" in names
        sources = dict(playbooks)
        assert sources["custom"] == "project"

    def test_deduplicates_by_name(self, tmp_path):
        workflows_dir = tmp_path / ".leashd" / "workflows"
        workflows_dir.mkdir(parents=True)
        data = {
            "name": "Override LinkedIn",
            "recipe": "linkedin_comment",
            "platform": "LinkedIn",
        }
        (workflows_dir / "linkedin_comment.yaml").write_text(yaml.dump(data))

        playbooks = list_playbooks(str(tmp_path))
        linkedin_entries = [(n, s) for n, s in playbooks if n == "linkedin_comment"]
        assert len(linkedin_entries) == 1
        assert linkedin_entries[0][1] == "project"

    def test_no_playbooks_in_empty_dir(self, tmp_path):
        # Bundled dir still exists, so there should be at least that
        playbooks = list_playbooks(str(tmp_path))
        assert len(playbooks) >= 1


# --- format_playbook_instruction ---


class TestFormatPlaybookInstruction:
    def test_basic_format(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="TestPlatform",
            url_patterns={"search": "https://example.com/search?q={topic}"},
            element_patterns={"button": "Submit button"},
            phases=[
                PlaybookPhase(
                    name="navigate",
                    description="Navigate to site",
                    steps=[
                        PlaybookStep(
                            action="navigate",
                            description="Open search page",
                            target="https://example.com",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb, topic="AI")
        assert "NAVIGATION GUIDE (TestPlatform)" in result
        assert "https://example.com/search?q=AI" in result
        assert "Submit button" in result
        assert "Phase: navigate" in result
        assert "Open search page" in result

    def test_topic_substitution_in_urls(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            url_patterns={"search": "https://site.com/?q={topic}"},
        )
        result = format_playbook_instruction(pb, topic="machine learning")
        assert "https://site.com/?q=machine learning" in result

    def test_no_topic_leaves_missing_placeholder(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            url_patterns={"search": "https://site.com/?q={topic}"},
        )
        result = format_playbook_instruction(pb, topic=None)
        assert "<MISSING_TOPIC>" in result
        assert "{topic}" not in result

    def test_step_details_included(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="click",
                            description="Click submit",
                            target="submit_button",
                            value="Submit",
                            expected_state="Form submitted",
                            notes="Wait for confirmation",
                            fallback="Retry once",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb)
        assert "Target: submit_button" in result
        assert "Value: Submit" in result
        assert "Expect: Form submitted" in result
        assert "Note: Wait for confirmation" in result
        assert "Fallback: Retry once" in result

    def test_empty_playbook(self):
        pb = Playbook(name="Empty", recipe="empty", platform="None")
        result = format_playbook_instruction(pb)
        assert "NAVIGATION GUIDE (None)" in result

    def test_topic_substitution_in_step_value(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="search",
                    steps=[
                        PlaybookStep(
                            action="type",
                            description="Enter search term",
                            value="{topic}",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb, topic="AI safety")
        assert "Value: AI safety" in result

    def test_tool_hint_rendered(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="click",
                            description="Click submit",
                            tool_hint="browser_click",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb)
        assert "Tool: browser_click" in result

    def test_verify_false_rendering(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="type",
                            description="Type text",
                            verify=False,
                            expected_state="Text in input",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb)
        assert "(no verification needed)" in result
        # expected_state should be suppressed when verify=False
        assert "Expect:" not in result

    def test_verify_true_shows_expected_state(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="click",
                            description="Click submit",
                            verify=True,
                            expected_state="Submitted",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb)
        assert "(no verification needed)" not in result
        assert "Expect: Submitted" in result

    def test_script_rendered(self):
        pb = Playbook(
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
        result = format_playbook_instruction(pb)
        assert "Script: document.title" in result

    def test_inline_guidance_rendered(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            inline_guidance="Use short comments. Be specific.",
        )
        result = format_playbook_instruction(pb)
        assert "COMMENT DRAFTING GUIDE:" in result
        assert "Use short comments. Be specific." in result

    def test_inline_guidance_before_phases(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            inline_guidance="Guide content here.",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(action="click", description="Click"),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb)
        guide_pos = result.index("COMMENT DRAFTING GUIDE:")
        phase_pos = result.index("Phase: action")
        assert guide_pos < phase_pos

    def test_backend_tool_hint_translation_playwright(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="click",
                            description="Click submit",
                            tool_hint="browser_click",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb, browser_backend="playwright")
        assert "Tool: browser_click" in result

    def test_backend_tool_hint_translation_agent_browser(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="click",
                            description="Click submit",
                            tool_hint="browser_click",
                        ),
                        PlaybookStep(
                            action="snapshot",
                            description="Take snapshot",
                            tool_hint="browser_snapshot",
                        ),
                        PlaybookStep(
                            action="evaluate",
                            description="Run script",
                            tool_hint="browser_evaluate",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb, browser_backend="agent-browser")
        assert "Tool: agent-browser click" in result
        assert "Tool: agent-browser snapshot -i" in result
        assert "Tool: agent-browser eval" in result
        assert "browser_click" not in result
        assert "browser_snapshot" not in result
        assert "browser_evaluate" not in result

    def test_non_browser_tool_hint_unchanged_by_backend(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(
                            action="ask",
                            description="Ask user",
                            tool_hint="AskUserQuestion",
                        ),
                    ],
                ),
            ],
        )
        result = format_playbook_instruction(pb, browser_backend="agent-browser")
        assert "Tool: AskUserQuestion" in result


# --- Bundled LinkedIn playbook structure ---


class TestBundledLinkedInPlaybook:
    @pytest.fixture
    def linkedin_playbook(self, tmp_path):
        pb = load_playbook(str(tmp_path), "linkedin_comment")
        assert pb is not None
        return pb

    def test_has_required_fields(self, linkedin_playbook):
        assert linkedin_playbook.name
        assert linkedin_playbook.recipe == "linkedin_comment"
        assert linkedin_playbook.platform == "LinkedIn"

    def test_has_search_url_pattern(self, linkedin_playbook):
        assert "search" in linkedin_playbook.url_patterns
        assert "{topic}" in linkedin_playbook.url_patterns["search"]

    def test_has_element_patterns(self, linkedin_playbook):
        assert "comment_button" in linkedin_playbook.element_patterns
        assert "comment_input" in linkedin_playbook.element_patterns
        assert "submit_comment" in linkedin_playbook.element_patterns

    def test_has_phases(self, linkedin_playbook):
        phase_names = [p.name for p in linkedin_playbook.phases]
        assert "authentication" in phase_names
        assert "search" in phase_names
        assert "scan_and_select" in phase_names
        assert "comment_on_post" in phase_names

    def test_scan_and_select_phase_exists(self, linkedin_playbook):
        phase = next(p for p in linkedin_playbook.phases if p.name == "scan_and_select")
        assert len(phase.steps) >= 2
        assert any("snapshot" in s.action for s in phase.steps)
        assert any("present" in s.action for s in phase.steps)

    def test_comment_phase_has_tool_hints(self, linkedin_playbook):
        phase = next(p for p in linkedin_playbook.phases if p.name == "comment_on_post")
        tool_hints = [s.tool_hint for s in phase.steps if s.tool_hint]
        assert "browser_click" in tool_hints
        assert "browser_type" in tool_hints

    def test_comment_phase_has_stop_step(self, linkedin_playbook):
        phase = next(p for p in linkedin_playbook.phases if p.name == "comment_on_post")
        assert any(s.action == "stop" for s in phase.steps)

    def test_comment_phase_intermediate_steps_no_verify(self, linkedin_playbook):
        phase = next(p for p in linkedin_playbook.phases if p.name == "comment_on_post")
        no_verify_steps = [s for s in phase.steps if not s.verify]
        assert len(no_verify_steps) >= 3

    def test_has_comment_input_selector(self, linkedin_playbook):
        assert "comment_input_selector" in linkedin_playbook.element_patterns

    def test_search_phase_uses_direct_url(self, linkedin_playbook):
        search_phase = next(p for p in linkedin_playbook.phases if p.name == "search")
        assert any("direct" in s.description.lower() for s in search_phase.steps)

    def test_no_inline_guidance(self, linkedin_playbook):
        assert linkedin_playbook.inline_guidance is None

    def test_comment_phase_has_scripts(self, linkedin_playbook):
        comment_phase = next(
            p for p in linkedin_playbook.phases if p.name == "comment_on_post"
        )
        steps_with_scripts = [s for s in comment_phase.steps if s.script]
        assert len(steps_with_scripts) >= 2

    def test_scan_phase_has_extraction_script(self, linkedin_playbook):
        scan_phase = next(
            p for p in linkedin_playbook.phases if p.name == "scan_and_select"
        )
        steps_with_scripts = [s for s in scan_phase.steps if s.script]
        assert len(steps_with_scripts) >= 1
        assert "feed-shared-update-v2" in steps_with_scripts[0].script

    def test_comment_type_step_has_no_script(self, linkedin_playbook):
        comment_phase = next(
            p for p in linkedin_playbook.phases if p.name == "comment_on_post"
        )
        type_step = next(s for s in comment_phase.steps if s.action == "type")
        assert type_step.script is None
        assert "NEVER" in type_step.notes

    def test_comment_phase_has_wait_step(self, linkedin_playbook):
        comment_phase = next(
            p for p in linkedin_playbook.phases if p.name == "comment_on_post"
        )
        actions = [s.action for s in comment_phase.steps]
        type_idx = actions.index("type")
        submit_idx = actions.index("submit")
        assert "wait" in actions[type_idx + 1 : submit_idx + 1]

    def test_submit_step_has_fallback(self, linkedin_playbook):
        comment_phase = next(
            p for p in linkedin_playbook.phases if p.name == "comment_on_post"
        )
        submit_step = next(s for s in comment_phase.steps if s.action == "submit")
        assert submit_step.fallback is not None
        assert "Enter" in submit_step.fallback

    def test_format_with_topic(self, linkedin_playbook):
        result = format_playbook_instruction(linkedin_playbook, topic="AI")
        assert "NAVIGATION GUIDE (LinkedIn)" in result
        assert "AI" in result


class TestPlaybookRequiresTopic:
    def test_topic_in_url(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            url_patterns={"search": "https://example.com/search?q={topic}"},
        )
        assert playbook_requires_topic(pb) is True

    def test_no_topic_anywhere(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            url_patterns={"home": "https://example.com"},
            phases=[
                PlaybookPhase(
                    name="action",
                    steps=[
                        PlaybookStep(action="click", description="Click"),
                    ],
                ),
            ],
        )
        assert playbook_requires_topic(pb) is False

    def test_topic_in_step_value(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="search",
                    steps=[
                        PlaybookStep(
                            action="type",
                            description="Enter search",
                            value="{topic}",
                        ),
                    ],
                ),
            ],
        )
        assert playbook_requires_topic(pb) is True

    def test_topic_in_step_target(self):
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[
                PlaybookPhase(
                    name="nav",
                    steps=[
                        PlaybookStep(
                            action="navigate",
                            description="Go to search",
                            target="https://example.com/?q={topic}",
                        ),
                    ],
                ),
            ],
        )
        assert playbook_requires_topic(pb) is True


# --- BackendStepOverride ---


class TestBackendStepOverride:
    def test_all_fields_optional(self):
        override = BackendStepOverride()
        assert override.description is None
        assert override.tool_hint is None
        assert override.script is None

    def test_frozen(self):
        from pydantic import ValidationError

        override = BackendStepOverride(notes="test")
        with pytest.raises(ValidationError, match="frozen"):
            override.notes = "other"  # type: ignore[misc]

    def test_model_fields_set_tracks_explicit_null(self):
        override = BackendStepOverride.model_validate({"script": None})
        assert "script" in override.model_fields_set
        assert override.script is None


# --- resolve_step ---


class TestResolveStep:
    def test_no_backends_returns_same(self):
        step = PlaybookStep(action="click", description="Click")
        result = resolve_step(step, "agent-browser")
        assert result is step

    def test_unknown_backend_returns_same(self):
        step = PlaybookStep(
            action="click",
            description="Click",
            backends={"agent-browser": BackendStepOverride(notes="ab notes")},
        )
        result = resolve_step(step, "selenium")
        assert result is step

    def test_override_merges_fields(self):
        step = PlaybookStep(
            action="click",
            description="Click submit",
            notes="base notes",
            tool_hint="browser_click",
            backends={
                "agent-browser": BackendStepOverride(
                    description="Click via ref",
                    notes="agent-browser click @eN",
                )
            },
        )
        result = resolve_step(step, "agent-browser")
        assert result.action == "click"
        assert result.description == "Click via ref"
        assert result.notes == "agent-browser click @eN"
        assert result.tool_hint == "browser_click"
        assert result.verify is True

    def test_explicit_null_removes_script(self):
        step = PlaybookStep(
            action="evaluate",
            description="Run script",
            script="document.title",
            backends={
                "agent-browser": BackendStepOverride.model_validate({"script": None})
            },
        )
        result = resolve_step(step, "agent-browser")
        assert result.script is None

    def test_unset_fields_keep_base(self):
        step = PlaybookStep(
            action="click",
            description="Click submit",
            tool_hint="browser_click",
            notes="base notes",
            backends={"agent-browser": BackendStepOverride(notes="override notes")},
        )
        result = resolve_step(step, "agent-browser")
        assert result.description == "Click submit"
        assert result.tool_hint == "browser_click"
        assert result.notes == "override notes"


# --- format_playbook_instruction with backend overrides ---


class TestFormatWithBackendOverrides:
    def test_override_tool_hint_used_verbatim(self):
        step = PlaybookStep(
            action="scroll",
            description="Scroll to post",
            tool_hint="browser_evaluate",
            backends={
                "agent-browser": BackendStepOverride(
                    tool_hint="agent-browser scrollintoview",
                )
            },
        )
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[PlaybookPhase(name="action", steps=[step])],
        )
        result = format_playbook_instruction(pb, browser_backend="agent-browser")
        assert "Tool: agent-browser scrollintoview" in result
        assert "Tool: agent-browser eval" not in result

    def test_no_override_still_translates(self):
        step = PlaybookStep(
            action="click",
            description="Click submit",
            tool_hint="browser_click",
        )
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[PlaybookPhase(name="action", steps=[step])],
        )
        result = format_playbook_instruction(pb, browser_backend="agent-browser")
        assert "Tool: agent-browser click" in result

    def test_null_script_suppressed(self):
        step = PlaybookStep(
            action="snapshot",
            description="Take snapshot",
            tool_hint="browser_snapshot",
            script="document.title",
            backends={
                "agent-browser": BackendStepOverride.model_validate({"script": None})
            },
        )
        pb = Playbook(
            name="Test",
            recipe="test",
            platform="Test",
            phases=[PlaybookPhase(name="action", steps=[step])],
        )
        result = format_playbook_instruction(pb, browser_backend="agent-browser")
        assert "Script:" not in result


# --- Bundled LinkedIn playbook: agent-browser override tests ---


class TestBundledLinkedInPlaybookOverrides:
    @pytest.fixture
    def linkedin_playbook(self, tmp_path):
        pb = load_playbook(str(tmp_path), "linkedin_comment")
        assert pb is not None
        return pb

    def test_scan_step_has_agent_browser_override(self, linkedin_playbook):
        scan_phase = next(
            p for p in linkedin_playbook.phases if p.name == "scan_and_select"
        )
        snapshot_step = next(s for s in scan_phase.steps if s.action == "snapshot")
        assert snapshot_step.backends is not None
        assert "agent-browser" in snapshot_step.backends
        override = snapshot_step.backends["agent-browser"]
        assert "script" in override.model_fields_set
        assert override.script is None

    def test_comment_steps_have_agent_browser_overrides(self, linkedin_playbook):
        comment_phase = next(
            p for p in linkedin_playbook.phases if p.name == "comment_on_post"
        )
        actions_with_overrides = [
            s.action
            for s in comment_phase.steps
            if s.backends and "agent-browser" in s.backends
        ]
        assert "scroll" in actions_with_overrides
        assert "click" in actions_with_overrides
        assert "wait" in actions_with_overrides
        assert "submit" in actions_with_overrides

    def test_format_agent_browser_uses_native_commands(self, linkedin_playbook):
        result = format_playbook_instruction(
            linkedin_playbook, topic="AI", browser_backend="agent-browser"
        )
        assert "agent-browser scrollintoview" in result
        assert "agent-browser wait" in result
        assert "agent-browser find" in result
        # Scan step should NOT have JS extraction script
        scan_section_start = result.index("Phase: scan_and_select")
        comment_section_start = result.index("Phase: comment_on_post")
        scan_section = result[scan_section_start:comment_section_start]
        assert "feed-shared-update-v2" not in scan_section
