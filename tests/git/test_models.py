"""Tests for git data models."""

import pytest
from pydantic import ValidationError

from leashd.git.models import (
    FileChange,
    GitBranch,
    GitLogEntry,
    GitResult,
    GitStatus,
    MergeResult,
)


class TestFileChange:
    def test_create_with_valid_data(self):
        fc = FileChange(path="src/app.py", status="modified")
        assert fc.path == "src/app.py"
        assert fc.status == "modified"

    def test_frozen_immutability(self):
        fc = FileChange(path="src/app.py", status="modified")
        with pytest.raises(ValidationError):
            fc.path = "other.py"  # type: ignore[misc]

    def test_all_statuses(self):
        for status in (
            "modified",
            "added",
            "deleted",
            "renamed",
            "copied",
            "untracked",
            "conflicted",
        ):
            fc = FileChange(path="file.txt", status=status)
            assert fc.status == status

    def test_rejects_invalid_status(self):
        with pytest.raises(ValidationError):
            FileChange(path="file.txt", status="unknown_status")

    def test_requires_path(self):
        with pytest.raises(ValidationError):
            FileChange(status="modified")  # type: ignore[call-arg]

    def test_requires_status(self):
        with pytest.raises(ValidationError):
            FileChange(path="file.txt")  # type: ignore[call-arg]


class TestGitStatus:
    def test_create_minimal(self):
        gs = GitStatus(branch="main")
        assert gs.branch == "main"
        assert gs.tracking is None
        assert gs.ahead == 0
        assert gs.behind == 0
        assert gs.staged == []
        assert gs.unstaged == []
        assert gs.untracked == []

    def test_create_full(self):
        gs = GitStatus(
            branch="feature/auth",
            tracking="origin/feature/auth",
            ahead=3,
            behind=1,
            staged=[FileChange(path="a.py", status="modified")],
            unstaged=[FileChange(path="b.py", status="deleted")],
            untracked=["new.txt"],
        )
        assert gs.branch == "feature/auth"
        assert gs.tracking == "origin/feature/auth"
        assert gs.ahead == 3
        assert gs.behind == 1
        assert len(gs.staged) == 1
        assert gs.staged[0].path == "a.py"
        assert len(gs.unstaged) == 1
        assert gs.untracked == ["new.txt"]

    def test_frozen_immutability(self):
        gs = GitStatus(branch="main")
        with pytest.raises(ValidationError):
            gs.branch = "develop"  # type: ignore[misc]

    def test_defaults_are_independent(self):
        gs1 = GitStatus(branch="main")
        gs2 = GitStatus(branch="develop")
        assert gs1.staged is not gs2.staged
        assert gs1.unstaged is not gs2.unstaged

    def test_requires_branch(self):
        with pytest.raises(ValidationError):
            GitStatus()  # type: ignore[call-arg]


class TestGitBranch:
    def test_create_minimal(self):
        gb = GitBranch(name="main")
        assert gb.name == "main"
        assert gb.is_current is False
        assert gb.is_remote is False

    def test_current_branch(self):
        gb = GitBranch(name="main", is_current=True)
        assert gb.is_current is True

    def test_remote_branch(self):
        gb = GitBranch(name="remotes/origin/main", is_remote=True)
        assert gb.is_remote is True
        assert gb.name == "remotes/origin/main"

    def test_frozen_immutability(self):
        gb = GitBranch(name="main")
        with pytest.raises(ValidationError):
            gb.name = "develop"  # type: ignore[misc]


class TestGitLogEntry:
    def test_create(self):
        entry = GitLogEntry(
            hash="abc123def456",
            short_hash="abc123d",
            author="Alice",
            date="2 hours ago",
            message="fix: handle edge case",
        )
        assert entry.hash == "abc123def456"
        assert entry.short_hash == "abc123d"
        assert entry.author == "Alice"
        assert entry.date == "2 hours ago"
        assert entry.message == "fix: handle edge case"

    def test_frozen_immutability(self):
        entry = GitLogEntry(
            hash="abc", short_hash="a", author="X", date="now", message="msg"
        )
        with pytest.raises(ValidationError):
            entry.message = "new msg"  # type: ignore[misc]

    def test_requires_all_fields(self):
        with pytest.raises(ValidationError):
            GitLogEntry(hash="abc", short_hash="a")  # type: ignore[call-arg]


class TestGitResult:
    def test_success_result(self):
        r = GitResult(success=True, message="Operation succeeded")
        assert r.success is True
        assert r.message == "Operation succeeded"
        assert r.details == ""

    def test_failure_result_with_details(self):
        r = GitResult(success=False, message="Failed", details="error output here")
        assert r.success is False
        assert r.details == "error output here"

    def test_frozen_immutability(self):
        r = GitResult(success=True, message="ok")
        with pytest.raises(ValidationError):
            r.success = False  # type: ignore[misc]

    def test_default_details(self):
        r = GitResult(success=True, message="ok")
        assert r.details == ""


class TestMergeResult:
    def test_create_success(self):
        r = MergeResult(success=True, message="Merged 'feat' into main")
        assert r.success is True
        assert r.had_conflicts is False
        assert r.conflicted_files == []
        assert r.details == ""

    def test_create_with_conflicts(self):
        r = MergeResult(
            success=False,
            had_conflicts=True,
            conflicted_files=["a.py", "b.py"],
            message="Merge conflicts detected",
            details="CONFLICT in a.py",
        )
        assert r.success is False
        assert r.had_conflicts is True
        assert len(r.conflicted_files) == 2
        assert r.details == "CONFLICT in a.py"

    def test_frozen_immutability(self):
        r = MergeResult(success=True, message="ok")
        with pytest.raises(ValidationError):
            r.success = False

    def test_defaults(self):
        r = MergeResult(success=True, message="ok")
        assert r.had_conflicts is False
        assert r.conflicted_files == []
        assert r.details == ""

    def test_requires_success_and_message(self):
        with pytest.raises(ValidationError):
            MergeResult()

    def test_conflicted_files_independent_instances(self):
        """Each instance has its own list, no shared state."""
        r1 = MergeResult(success=False, message="m1", conflicted_files=["a.py"])
        r2 = MergeResult(success=False, message="m2", conflicted_files=["b.py"])
        assert r1.conflicted_files != r2.conflicted_files
        assert r1.conflicted_files == ["a.py"]
        assert r2.conflicted_files == ["b.py"]
