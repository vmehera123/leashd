"""Tests for TaskMemory persistent working-memory files."""

from __future__ import annotations

import pytest

from leashd.core import task_memory


class TestSeed:
    def test_creates_file(self, tmp_path):
        fp = task_memory.seed("abc123", "Add a hello endpoint", str(tmp_path))
        assert fp.is_file()
        assert fp.name == "abc123.md"

    def test_content_contains_task(self, tmp_path):
        task_memory.seed("r1", "Fix the login bug", str(tmp_path))
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "Fix the login bug" in content
        assert "r1" in content

    def test_truncates_long_task_title(self, tmp_path):
        long_task = "x" * 200
        task_memory.seed("r2", long_task, str(tmp_path))
        content = task_memory.read("r2", str(tmp_path))
        assert content is not None
        assert "..." in content

    def test_creates_tasks_subdirectory(self, tmp_path):
        task_memory.seed("r3", "Test", str(tmp_path))
        assert (tmp_path / ".leashd" / "tasks").is_dir()


class TestPathAndExists:
    def test_path_returns_expected_location(self, tmp_path):
        p = task_memory.path("abc", str(tmp_path))
        assert p.name == "abc.md"
        assert ".leashd/tasks" in str(p)

    def test_path_rejects_path_traversal_in_run_id(self, tmp_path):
        """Run IDs with path separators or '..' could escape .leashd/tasks/."""
        for bad_id in ["../../../etc/passwd", "foo/bar", "foo\\bar", "a..b"]:
            with pytest.raises(ValueError, match="Invalid run_id"):
                task_memory.path(bad_id, str(tmp_path))

    def test_exists_false_before_seed(self, tmp_path):
        assert not task_memory.exists("nope", str(tmp_path))

    def test_exists_true_after_seed(self, tmp_path):
        task_memory.seed("yes", "task", str(tmp_path))
        assert task_memory.exists("yes", str(tmp_path))


class TestRead:
    def test_returns_none_if_missing(self, tmp_path):
        assert task_memory.read("missing", str(tmp_path)) is None

    def test_returns_full_content_if_small(self, tmp_path):
        task_memory.seed("r1", "small task", str(tmp_path))
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "## Checkpoint" in content

    def test_truncation_preserves_head_and_tail(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        # Append a large progress section so the file exceeds max_chars
        content = fp.read_text(encoding="utf-8")
        content += "\n- action " + "x" * 10000
        content += "\n## Checkpoint\nNext: test | Retries: 1 | Blocked: none\n"
        fp.write_text(content, encoding="utf-8")
        result = task_memory.read("r1", str(tmp_path), max_chars=800)
        assert result is not None
        # Head: task description and template sections preserved
        assert "# Task:" in result
        # Tail: checkpoint section preserved
        assert "Checkpoint" in result or "test" in result
        # Truncation marker present (rewritten to include path hint).
        assert "middle truncated" in result

    def test_returns_none_when_file_unreadable(self, tmp_path):
        """File permissions lost mid-task (NFS mount drop, chmod by agent)."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        fp.chmod(0o000)
        try:
            result = task_memory.read("r1", str(tmp_path))
            assert result is None
        finally:
            fp.chmod(0o644)

    def test_read_truncation_snaps_to_newline_when_no_progress_boundary(self, tmp_path):
        """Large Assessment pushes Progress deep — head must snap to newline."""
        task_memory.seed("r1", "implement feature", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        # Replace assessment with a large block so Progress starts beyond head_budget
        big_assessment = "Assessment line\n" * 100  # ~1600 chars
        content = content.replace(
            "(pending — the orchestrator will assess complexity on the first action)",
            big_assessment,
        )
        fp.write_text(content, encoding="utf-8")

        result = task_memory.read("r1", str(tmp_path), max_chars=800)
        assert result is not None
        assert "middle truncated" in result
        # Head should end at a newline boundary. The marker now starts
        # with "[..." so split on that prefix; head_part is the pure
        # pre-marker content and must end with a newline.
        head_part = result.split("[...middle truncated")[0]
        assert head_part.endswith("\n")

    def test_head_preserves_plan_section(self, tmp_path):
        task_memory.seed("r1", "implement feature", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace(
            "(no plan yet)", "Step 1: create module\nStep 2: add tests"
        )
        # Add lots of progress to push past max_chars
        content += "\n| 1 | explore | done | 10s |\n" * 200
        fp.write_text(content, encoding="utf-8")
        result = task_memory.read("r1", str(tmp_path), max_chars=2000)
        assert result is not None
        assert "## Plan" in result
        assert "create module" in result


class TestGetCheckpoint:
    def test_returns_empty_dict_if_missing(self, tmp_path):
        assert task_memory.get_checkpoint("missing", str(tmp_path)) == {}

    def test_returns_empty_dict_when_checkpoint_heading_removed(self, tmp_path):
        """File exists but agent removed the ## Checkpoint heading entirely."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace("## Checkpoint", "## Removed")
        fp.write_text(content, encoding="utf-8")
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp == {}

    def test_parses_default_checkpoint(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["next"] == "pending"
        assert cp["retries"] == "0"
        assert cp["blocked"] == "none"

    def test_empty_checkpoint_body_returns_empty_dict(self, tmp_path):
        """Agent cleared checkpoint content but left the heading — don't crash."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        # Replace the checkpoint line with only blank lines
        content = content.replace(
            "Next: pending | Retries: 0 | Blocked: none",
            "\n\n",
        )
        fp.write_text(content, encoding="utf-8")
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp == {}

    def test_parses_custom_checkpoint(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace(
            "Next: pending | Retries: 0 | Blocked: none",
            "Next: test | Retries: 2 | Blocked: waiting for API key",
        )
        fp.write_text(content, encoding="utf-8")
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["next"] == "test"
        assert cp["retries"] == "2"
        assert cp["blocked"] == "waiting for API key"

    def test_parses_multiline_checkpoint(self, tmp_path):
        """Multi-line checkpoint with Completed/Pending lines."""
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_checkpoint(
            "r1",
            str(tmp_path),
            next_phase="after-implement",
            retries=1,
            git_hash="abc1234",
            completed_phases=["explore", "plan", "implement"],
            pending_phases=["test", "verify", "review"],
        )
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["next"] == "after-implement"
        assert cp["retries"] == "1"
        assert cp["commit"] == "abc1234"
        assert cp["completed"] == "explore, plan, implement"
        assert cp["pending"] == "test, verify, review"

    def test_multiline_checkpoint_stops_at_next_section(self, tmp_path):
        """Parser must not read past the next ## heading."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        # Append a fake section after checkpoint
        content += "\n## Extra\nKey: should-not-appear\n"
        fp.write_text(content, encoding="utf-8")
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert "key" not in cp


class TestAppendProgressRow:
    def test_appends_first_row(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.append_progress_row(
            "r1", str(tmp_path), action="explore", result="done", elapsed="12s"
        )
        assert ok is True
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "| 1 | explore | done | 12s |" in content

    def test_appends_multiple_rows(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.append_progress_row(
            "r1", str(tmp_path), action="explore", result="done", elapsed="10s"
        )
        task_memory.append_progress_row(
            "r1", str(tmp_path), action="plan", result="done", elapsed="8s"
        )
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "| 1 | explore |" in content
        assert "| 2 | plan |" in content

    def test_truncates_long_result(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.append_progress_row(
            "r1", str(tmp_path), action="test", result="x" * 200, elapsed="5s"
        )
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "..." in content

    def test_returns_false_when_file_unreadable(self, tmp_path):
        """File exists but becomes unreadable mid-task (permissions, NFS mount lost)."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        fp.chmod(0o000)
        try:
            ok = task_memory.append_progress_row(
                "r1", str(tmp_path), action="test", result="ok", elapsed="5s"
            )
            assert ok is False
        finally:
            fp.chmod(0o644)

    def test_returns_false_for_missing_file(self, tmp_path):
        ok = task_memory.append_progress_row(
            "missing", str(tmp_path), action="test", result="ok", elapsed="1s"
        )
        assert ok is False

    def test_preserves_other_sections(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.append_progress_row(
            "r1", str(tmp_path), action="explore", result="done", elapsed="10s"
        )
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "## Changes" in content
        assert "## Checkpoint" in content

    def test_returns_false_when_progress_section_missing(self, tmp_path):
        """Agent accidentally removed the ## Progress heading — must not crash."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace("## Progress", "## ProgressDeleted")
        fp.write_text(content, encoding="utf-8")
        ok = task_memory.append_progress_row(
            "r1", str(tmp_path), action="test", result="ok", elapsed="5s"
        )
        assert ok is False

    def test_appends_when_progress_is_last_section(self, tmp_path):
        """Agent deleted all sections after Progress — row inserts at end of file."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        # Remove everything after the Progress table header
        progress_idx = content.find("## Progress")
        table_end = content.find("\n\n## ", progress_idx + 1)
        if table_end != -1:
            content = content[:table_end] + "\n"
        fp.write_text(content, encoding="utf-8")

        ok = task_memory.append_progress_row(
            "r1",
            str(tmp_path),
            action="explore",
            result="mapped codebase",
            elapsed="15s",
        )
        assert ok is True
        result = fp.read_text(encoding="utf-8")
        assert "| 1 | explore | mapped codebase | 15s |" in result

    def test_does_not_duplicate_with_agent_rows(self, tmp_path):
        """Orchestrator rows coexist with rows the agent wrote."""
        task_memory.seed("r1", "task", str(tmp_path))
        # Simulate agent adding a row manually
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace(
            "|---|--------|--------|------|\n",
            "|---|--------|--------|------|\n| 1 | explore | mapped files | 5s |\n",
        )
        fp.write_text(content, encoding="utf-8")
        # Orchestrator appends — should be row 2
        task_memory.append_progress_row(
            "r1", str(tmp_path), action="explore", result="done", elapsed="10s"
        )
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "| 1 | explore | mapped files" in content
        assert "| 2 | explore | done" in content


class TestUpdateCheckpoint:
    def test_updates_checkpoint_section(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_checkpoint(
            "r1", str(tmp_path), next_phase="test", retries=1
        )
        assert ok is True
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["next"] == "test"
        assert cp["retries"] == "1"

    def test_includes_git_hash(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_checkpoint(
            "r1", str(tmp_path), next_phase="review", git_hash="abc1234"
        )
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["commit"] == "abc1234"

    def test_updates_timestamp(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        before = fp.read_text(encoding="utf-8")
        task_memory.update_checkpoint("r1", str(tmp_path), next_phase="implement")
        after = fp.read_text(encoding="utf-8")
        # Updated timestamp should have changed
        assert before != after

    def test_returns_false_for_missing_file(self, tmp_path):
        ok = task_memory.update_checkpoint("missing", str(tmp_path), next_phase="test")
        assert ok is False

    def test_returns_false_when_file_unreadable(self, tmp_path):
        """Permissions lost on the memory file during task execution."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        fp.chmod(0o000)
        try:
            ok = task_memory.update_checkpoint("r1", str(tmp_path), next_phase="test")
            assert ok is False
        finally:
            fp.chmod(0o644)

    def test_returns_false_when_checkpoint_section_missing(self, tmp_path):
        """Corrupted file without ## Checkpoint heading — orchestrator must know."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace("## Checkpoint", "## Deleted")
        fp.write_text(content, encoding="utf-8")
        ok = task_memory.update_checkpoint("r1", str(tmp_path), next_phase="test")
        assert ok is False

    def test_inserts_line_when_checkpoint_body_is_empty(self, tmp_path):
        """Empty section body — insert new checkpoint instead of silently skipping."""
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        # Make the checkpoint section empty (heading with only whitespace after)
        content = content.replace(
            "Next: pending | Retries: 0 | Blocked: none",
            "   \n   ",
        )
        fp.write_text(content, encoding="utf-8")
        ok = task_memory.update_checkpoint(
            "r1", str(tmp_path), next_phase="implement", retries=1
        )
        assert ok is True
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["next"] == "implement"
        assert cp["retries"] == "1"

    def test_preserves_blocked_field(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_checkpoint(
            "r1", str(tmp_path), next_phase="fix", blocked="test failures"
        )
        cp = task_memory.get_checkpoint("r1", str(tmp_path))
        assert cp["blocked"] == "test failures"

    def test_includes_completed_and_pending_phases(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_checkpoint(
            "r1",
            str(tmp_path),
            next_phase="after-implement",
            completed_phases=["plan", "implement"],
            pending_phases=["test", "verify", "review"],
        )
        assert ok is True
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "Completed: plan, implement" in content
        assert "Pending: test, verify, review" in content

    def test_empty_completed_phases(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_checkpoint(
            "r1",
            str(tmp_path),
            next_phase="after-plan",
            completed_phases=[],
            pending_phases=["implement", "test"],
        )
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "Completed: none" in content
        assert "Pending: implement, test" in content

    def test_phase_status_preserves_other_sections(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_checkpoint(
            "r1",
            str(tmp_path),
            next_phase="after-implement",
            completed_phases=["plan"],
            pending_phases=["test"],
        )
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        # Checkpoint is the last section, but verify it doesn't
        # eat into earlier sections
        assert "## Progress" in content
        assert "## Changes" in content


class TestUpdateSection:
    def test_replaces_placeholder_section(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Plan",
            content="Step 1: create module\nStep 2: add tests",
        )
        assert ok is True
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "Step 1: create module" in content
        assert "(no plan yet)" not in content

    def test_only_if_placeholder_skips_real_content(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace("(no plan yet)", "Already has a real plan")
        fp.write_text(content, encoding="utf-8")

        ok = task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Plan",
            content="Overwrite attempt",
            only_if_placeholder=True,
        )
        assert ok is False
        content = fp.read_text(encoding="utf-8")
        assert "Already has a real plan" in content

    def test_only_if_placeholder_replaces_placeholder(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Plan",
            content="New plan content",
            only_if_placeholder=True,
        )
        assert ok is True
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "New plan content" in content

    def test_preserves_subsequent_sections(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Plan",
            content="My detailed plan",
        )
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "## Progress" in content
        assert "## Changes" in content
        assert "## Checkpoint" in content

    def test_returns_false_for_missing_file(self, tmp_path):
        ok = task_memory.update_section(
            "missing", str(tmp_path), section="Plan", content="x"
        )
        assert ok is False

    def test_returns_false_for_missing_section(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_section(
            "r1", str(tmp_path), section="NonExistent", content="x"
        )
        assert ok is False

    def test_works_on_non_plan_sections(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Codebase Context",
            content="FastAPI app with SQLAlchemy ORM",
        )
        assert ok is True
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "FastAPI app with SQLAlchemy ORM" in content


class TestUpdateChangesSection:
    def test_replaces_placeholder(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        ok = task_memory.update_changes_section(
            "r1",
            str(tmp_path),
            diff_stat=" src/app.py | 10 ++-\n 1 file changed",
        )
        assert ok is True
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "src/app.py | 10 ++-" in content
        assert "(no changes yet)" not in content

    def test_preserves_subsequent_sections(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_changes_section(
            "r1",
            str(tmp_path),
            diff_stat="frontend/page.tsx | 5 +-",
        )
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "## Test Results" in content
        assert "## Verification" in content
        assert "## Checkpoint" in content

    def test_empty_diff_stat(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        task_memory.update_changes_section("r1", str(tmp_path), diff_stat="")
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        assert "(no changes detected)" in content

    def test_returns_false_for_missing_file(self, tmp_path):
        ok = task_memory.update_changes_section("missing", str(tmp_path), diff_stat="x")
        assert ok is False

    def test_returns_false_when_changes_section_missing(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        fp = task_memory.path("r1", str(tmp_path))
        content = fp.read_text(encoding="utf-8")
        content = content.replace("## Changes", "## Deleted")
        fp.write_text(content, encoding="utf-8")
        ok = task_memory.update_changes_section("r1", str(tmp_path), diff_stat="x")
        assert ok is False


class TestSeedV3Template:
    def test_v3_has_four_phase_sections(self, tmp_path):
        task_memory.seed("r1", "Build widget", str(tmp_path), version="v3")
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "## Plan" in content
        assert "## Implementation Summary" in content
        assert "## Verification" in content
        assert "## Review" in content

    def test_v3_omits_legacy_sections(self, tmp_path):
        task_memory.seed("r1", "Build widget", str(tmp_path), version="v3")
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        # v3 drops Assessment, Codebase Context, Changes, Test Results,
        # Review Notes in favor of the four phase sections.
        assert "## Assessment" not in content
        assert "## Codebase Context" not in content
        assert "## Test Results" not in content
        assert "## Review Notes" not in content

    def test_v3_checkpoint_seeded_with_pipeline(self, tmp_path):
        task_memory.seed("r1", "t", str(tmp_path), version="v3")
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "Next: plan" in content
        assert "Phase: plan" in content
        assert "Pending: plan, implement, verify, review" in content

    def test_default_version_keeps_legacy_layout(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path))
        content = task_memory.read("r1", str(tmp_path))
        assert content is not None
        assert "## Assessment" in content


class TestReadSection:
    def test_returns_none_for_missing_file(self, tmp_path):
        assert (
            task_memory.read_section("missing", str(tmp_path), section="Plan") is None
        )

    def test_returns_none_for_missing_section(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path), version="v3")
        assert (
            task_memory.read_section("r1", str(tmp_path), section="Nonexistent") is None
        )

    def test_reads_placeholder_body(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path), version="v3")
        body = task_memory.read_section("r1", str(tmp_path), section="Plan")
        assert body is not None
        assert task_memory.is_placeholder(body)

    def test_is_placeholder_helper(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path), version="v3")
        plan = task_memory.read_section("r1", str(tmp_path), section="Plan")
        assert task_memory.is_placeholder(plan)
        # Real content starting with '(' must NOT be misclassified.
        assert not task_memory.is_placeholder(
            "(Note: scope narrowed) Step 1: add route"
        )
        assert task_memory.is_placeholder(None)
        assert task_memory.is_placeholder("")
        assert task_memory.is_placeholder("   \n  ")

    def test_reads_populated_section(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path), version="v3")
        task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Plan",
            content="1. Do X\n2. Do Y",
        )
        body = task_memory.read_section("r1", str(tmp_path), section="Plan")
        assert body == "1. Do X\n2. Do Y"

    def test_stops_at_next_heading(self, tmp_path):
        task_memory.seed("r1", "task", str(tmp_path), version="v3")
        task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Plan",
            content="PLAN-BODY",
        )
        task_memory.update_section(
            "r1",
            str(tmp_path),
            section="Implementation Summary",
            content="IMPL-BODY",
        )
        plan = task_memory.read_section("r1", str(tmp_path), section="Plan")
        assert plan is not None
        assert "PLAN-BODY" in plan
        assert "IMPL-BODY" not in plan
