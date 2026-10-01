"""Tests for the /task markdown working memory."""

from __future__ import annotations

import pytest

from leashd.core import task_memory


@pytest.fixture
def run_id() -> str:
    return "abc123"


def _seed(run_id, tmp_path, phases=("implement", "verify"), task="Add /hello"):
    return task_memory.seed(run_id, task, str(tmp_path), phases=list(phases))


class TestPath:
    def test_path_under_tasks_dir(self, run_id, tmp_path):
        assert task_memory.path(run_id, str(tmp_path)) == (
            tmp_path / ".leashd" / "tasks" / f"{run_id}.md"
        )

    @pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b"])
    def test_rejects_traversal(self, bad, tmp_path):
        with pytest.raises(ValueError, match="Invalid run_id"):
            task_memory.path(bad, str(tmp_path))


class TestSeed:
    def test_creates_file_with_pipeline_sections(self, run_id, tmp_path):
        fp = _seed(run_id, tmp_path)
        content = fp.read_text()
        assert fp.is_file()
        assert "## Task Description\nAdd /hello" in content
        assert "## Implementation Summary" in content
        assert "## Verification" in content
        assert "## Review" not in content
        assert "Pending: implement, verify" in content
        assert "Next: implement" in content

    def test_review_section_only_when_in_pipeline(self, run_id, tmp_path):
        content = _seed(run_id, tmp_path, ("implement", "verify", "review")).read_text()
        assert "## Review" in content

    def test_truncates_long_title(self, run_id, tmp_path):
        content = _seed(run_id, tmp_path, task="x" * 200).read_text()
        assert f"# Task: {'x' * 80}..." in content

    def test_sections_start_as_placeholders(self, run_id, tmp_path):
        _seed(run_id, tmp_path)
        body = task_memory.read_section(
            run_id, str(tmp_path), section="Implementation Summary"
        )
        assert task_memory.is_placeholder(body)


class TestIsPlaceholder:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (None, True),
            ("", True),
            ("   ", True),
            ("<!-- pending:verify --> x", True),
            ("(parenthesised real content)", False),
            ("Status: PASS", False),
        ],
    )
    def test_detection(self, body, expected):
        assert task_memory.is_placeholder(body) is expected


class TestReadSection:
    def test_missing_file(self, run_id, tmp_path):
        assert task_memory.read_section(run_id, str(tmp_path), section="Review") is None

    def test_missing_section(self, run_id, tmp_path):
        _seed(run_id, tmp_path)
        assert task_memory.read_section(run_id, str(tmp_path), section="Review") is None

    def test_reads_body_until_next_heading(self, run_id, tmp_path):
        fp = _seed(run_id, tmp_path)
        fp.write_text(
            fp.read_text().replace(
                "<!-- pending:verify --> (checks, quality review, live check)",
                "Status: PASS\nVisual check: n/a",
            )
        )
        body = task_memory.read_section(run_id, str(tmp_path), section="Verification")
        assert body == "Status: PASS\nVisual check: n/a"


class TestCheckpoint:
    def test_missing_file(self, run_id, tmp_path):
        assert task_memory.get_checkpoint(run_id, str(tmp_path)) == {}
        assert (
            task_memory.update_checkpoint(run_id, str(tmp_path), next_phase="x")
            is False
        )

    def test_parses_seeded_checkpoint(self, run_id, tmp_path):
        _seed(run_id, tmp_path)
        checkpoint = task_memory.get_checkpoint(run_id, str(tmp_path))
        assert checkpoint["next"] == "implement"
        assert checkpoint["retries"] == "0"
        assert checkpoint["blocked"] == "none"
        assert checkpoint["pending"] == "implement, verify"

    def test_update_round_trips(self, run_id, tmp_path):
        _seed(run_id, tmp_path)
        assert task_memory.update_checkpoint(
            run_id,
            str(tmp_path),
            next_phase="verify",
            retries=1,
            blocked="flaky db",
            completed_phases=["implement"],
            pending_phases=["verify"],
        )
        checkpoint = task_memory.get_checkpoint(run_id, str(tmp_path))
        assert checkpoint == {
            "next": "verify",
            "retries": "1",
            "blocked": "flaky db",
            "completed": "implement",
            "pending": "verify",
        }

    def test_update_refreshes_timestamp(self, run_id, tmp_path):
        fp = _seed(run_id, tmp_path)
        fp.write_text(
            fp.read_text().replace("Updated: 20", "Updated: 1999-01-01T00:00:00Z X", 1)
        )
        task_memory.update_checkpoint(run_id, str(tmp_path), next_phase="verify")
        assert "Updated: 1999-01-01T00:00:00Z" not in fp.read_text()

    def test_update_without_checkpoint_heading(self, run_id, tmp_path):
        fp = _seed(run_id, tmp_path)
        fp.write_text(fp.read_text().replace("## Checkpoint", "## Gone"))
        assert (
            task_memory.update_checkpoint(run_id, str(tmp_path), next_phase="x")
            is False
        )
        assert task_memory.get_checkpoint(run_id, str(tmp_path)) == {}

    def test_checkpoint_stops_at_next_section(self, run_id, tmp_path):
        fp = _seed(run_id, tmp_path)
        fp.write_text(fp.read_text() + "\n## Notes\nOwner: someone\n")
        assert "owner" not in task_memory.get_checkpoint(run_id, str(tmp_path))
