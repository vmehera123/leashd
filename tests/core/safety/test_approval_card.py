"""Tests for the structured approval card built from a gated tool call."""

from leashd.core.safety.approval_card import build_approval_card
from leashd.core.safety.policy import Classification


def _classification(
    *,
    description="Shell access to a credential path",
    risk_level="critical",
    matched_command=None,
):
    return Classification(
        category="credential-bash",
        tool_name="Bash",
        tool_input={},
        risk_level=risk_level,
        description=description,
        matched_command=matched_command,
    )


def test_a_bash_card_carries_claudes_summary_and_the_command():
    """Claude describes every Bash call in words. The card never showed it,
    which left a phone user reading raw shell to work out what was asked."""
    card = build_approval_card(
        "Bash::source",
        {
            "command": "set -a; source .env; set +a",
            "description": "Load  the env\nfile",
        },
        _classification(),
        description="plain text prompt",
        working_directory="/w/protostar",
    )

    assert card.approval_key == "Bash::source"
    assert card.tool_name == "Bash"
    assert card.summary == "Load the env file"
    assert card.command == "set -a; source .env; set +a"
    assert card.reason == "Shell access to a credential path"
    assert card.risk_level == "critical"
    assert card.gated_segment == ""
    assert card.working_directory == "/w/protostar"
    assert card.description == "plain text prompt"


def test_a_compound_command_names_the_segment_the_policy_gated():
    card = build_approval_card(
        "Bash::rm",
        {"command": "cd build && rm -rf dist"},
        _classification(
            description="Compound command requires approval: Recursive delete",
            risk_level="high",
            matched_command="rm -rf dist",
        ),
        description="",
    )

    assert card.gated_segment == "rm -rf dist"
    assert card.reason == "Recursive delete"


def test_a_single_segment_command_has_no_separate_gated_segment():
    card = build_approval_card(
        "Bash::rm",
        {"command": "rm -rf dist"},
        _classification(matched_command="rm -rf dist"),
        description="",
    )

    assert card.gated_segment == ""


def test_an_unmatched_write_reads_as_outside_the_policy():
    card = build_approval_card(
        "Write",
        {"file_path": "/w/tools/probe.py", "content": "x = 1\n"},
        _classification(description="Unmatched tool call: Write", risk_level="medium"),
        description="",
    )

    assert card.reason == "Not covered by your policy"
    assert card.path == "/w/tools/probe.py"
    assert card.preview == "x = 1\n"
    assert card.preview_language == "python"
    assert card.command == ""
    assert card.details == ()


def test_an_edit_previews_as_a_diff():
    card = build_approval_card(
        "Edit",
        {"file_path": "/w/a.py", "old_string": "a = 1\nb = 2", "new_string": "a = 3"},
        _classification(),
        description="",
    )

    assert card.preview == "- a = 1\n- b = 2\n+ a = 3"
    assert card.preview_language == "diff"


def test_a_multiedit_previews_every_edit():
    card = build_approval_card(
        "MultiEdit",
        {
            "file_path": "/w/a.py",
            "edits": [
                {"old_string": "a", "new_string": "b"},
                {"old_string": "c", "new_string": "d"},
                "not an edit",
            ],
        },
        _classification(),
        description="",
    )

    assert card.preview == "- a\n+ b\n- c\n+ d"


def test_another_tool_lists_its_input():
    card = build_approval_card(
        "mcp__playwright__browser_click",
        {"element": "Submit", "ref": "e12", "options": {"force": True}, "note": ""},
        _classification(),
        description="",
    )

    assert card.tool_name == "mcp__playwright__browser_click"
    assert card.details == (
        ("element", "Submit"),
        ("ref", "e12"),
        ("options", '{"force": true}'),
    )


def test_a_long_input_value_is_clipped():
    card = build_approval_card(
        "WebFetch",
        {"url": "https://example.com/" + "a" * 500},
        _classification(),
        description="",
    )

    [(key, value)] = card.details
    assert key == "url"
    assert len(value) == 200
    assert value.endswith("…")
