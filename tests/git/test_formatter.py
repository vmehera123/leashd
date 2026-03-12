"""Tests for git display formatting functions."""

from leashd.git import formatter
from leashd.git.models import (
    FileChange,
    GitBranch,
    GitLogEntry,
    GitResult,
    GitStatus,
    MergeResult,
)


class TestFormatStatus:
    def test_clean_working_tree(self):
        status = GitStatus(branch="main")
        result = formatter.format_status(status)
        assert "main" in result
        assert "Working tree clean" in result

    def test_branch_with_tracking(self):
        status = GitStatus(branch="main", tracking="origin/main")
        result = formatter.format_status(status)
        assert "tracking origin/main" in result

    def test_branch_ahead_behind(self):
        status = GitStatus(branch="main", tracking="origin/main", ahead=3, behind=2)
        result = formatter.format_status(status)
        assert "3 ahead" in result
        assert "2 behind" in result

    def test_branch_only_ahead(self):
        status = GitStatus(branch="main", tracking="origin/main", ahead=5)
        result = formatter.format_status(status)
        assert "5 ahead" in result
        assert "behind" not in result

    def test_staged_files(self):
        status = GitStatus(
            branch="main",
            staged=[
                FileChange(path="src/app.py", status="modified"),
                FileChange(path="src/utils.py", status="added"),
            ],
        )
        result = formatter.format_status(status)
        assert "Staged:" in result
        assert "M src/app.py" in result
        assert "A src/utils.py" in result

    def test_unstaged_files(self):
        status = GitStatus(
            branch="main",
            unstaged=[FileChange(path="tests/test.py", status="modified")],
        )
        result = formatter.format_status(status)
        assert "Unstaged:" in result
        assert "M tests/test.py" in result

    def test_untracked_files(self):
        status = GitStatus(branch="main", untracked=["notes.txt", "scratch.py"])
        result = formatter.format_status(status)
        assert "Untracked:" in result
        assert "notes.txt" in result
        assert "scratch.py" in result

    def test_all_sections(self):
        status = GitStatus(
            branch="main",
            staged=[FileChange(path="a.py", status="modified")],
            unstaged=[FileChange(path="b.py", status="deleted")],
            untracked=["c.txt"],
        )
        result = formatter.format_status(status)
        assert "Staged:" in result
        assert "Unstaged:" in result
        assert "Untracked:" in result
        assert "Working tree clean" not in result

    def test_deleted_file_indicator(self):
        status = GitStatus(
            branch="main",
            unstaged=[FileChange(path="old.py", status="deleted")],
        )
        result = formatter.format_status(status)
        assert "D old.py" in result

    def test_renamed_file_indicator(self):
        status = GitStatus(
            branch="main",
            staged=[FileChange(path="new.py", status="renamed")],
        )
        result = formatter.format_status(status)
        assert "R new.py" in result

    def test_conflicted_file_indicator(self):
        status = GitStatus(
            branch="main",
            staged=[FileChange(path="conflict.py", status="conflicted")],
        )
        result = formatter.format_status(status)
        assert "U conflict.py" in result


class TestFormatBranches:
    def test_empty_branch_list(self):
        result = formatter.format_branches([])
        assert "No branches found" in result

    def test_single_branch(self):
        branches = [GitBranch(name="main", is_current=True)]
        result = formatter.format_branches(branches)
        assert "* main" in result
        assert "Local branches:" in result

    def test_multiple_branches(self):
        branches = [
            GitBranch(name="main", is_current=True),
            GitBranch(name="develop"),
            GitBranch(name="feature/auth"),
        ]
        result = formatter.format_branches(branches)
        assert "* main" in result
        assert "develop" in result
        assert "feature/auth" in result

    def test_truncation_at_max_display(self):
        branches = [GitBranch(name=f"branch-{i}") for i in range(15)]
        result = formatter.format_branches(branches, max_display=5)
        assert "... and 10 more" in result

    def test_no_truncation_when_within_limit(self):
        branches = [GitBranch(name=f"branch-{i}") for i in range(3)]
        result = formatter.format_branches(branches, max_display=10)
        assert "... and" not in result

    def test_search_tip_present(self):
        branches = [GitBranch(name="main")]
        result = formatter.format_branches(branches)
        assert "/git branch <query>" in result
        assert "/git checkout <branch-name>" in result

    def test_current_branch_marker(self):
        branches = [
            GitBranch(name="main", is_current=True),
            GitBranch(name="develop", is_current=False),
        ]
        result = formatter.format_branches(branches)
        lines = result.split("\n")
        main_line = next(line for line in lines if "main" in line)
        develop_line = next(line for line in lines if "develop" in line)
        assert main_line.startswith("* ")
        assert not develop_line.startswith("* ")


class TestFormatBranchSearch:
    def test_no_results(self):
        result = formatter.format_branch_search("xyz", [])
        assert 'No branches matching "xyz"' in result

    def test_with_results(self):
        branches = [
            GitBranch(name="feature/auth"),
            GitBranch(name="feature/dashboard"),
        ]
        result = formatter.format_branch_search("feat", branches)
        assert 'Branches matching "feat"' in result
        assert "2 found" in result
        assert "feature/auth" in result
        assert "feature/dashboard" in result

    def test_remote_branch_globe_prefix(self):
        branches = [
            GitBranch(name="remotes/origin/feature/auth", is_remote=True),
        ]
        result = formatter.format_branch_search("auth", branches)
        assert "\U0001f310" in result  # globe emoji

    def test_local_branch_no_globe(self):
        branches = [GitBranch(name="feature/auth", is_remote=False)]
        result = formatter.format_branch_search("auth", branches)
        assert "\U0001f310" not in result

    def test_truncation(self):
        branches = [GitBranch(name=f"feature/f-{i}") for i in range(15)]
        result = formatter.format_branch_search("feature", branches, max_display=5)
        assert "... and 10 more" in result


class TestFormatLog:
    def test_empty_log(self):
        result = formatter.format_log([])
        assert "No commits found" in result

    def test_single_entry(self):
        entries = [
            GitLogEntry(
                hash="abc123",
                short_hash="abc",
                author="Alice",
                date="2 hours ago",
                message="fix: auth bug",
            )
        ]
        result = formatter.format_log(entries)
        assert "abc fix: auth bug" in result
        assert "Alice, 2 hours ago" in result

    def test_multiple_entries(self):
        entries = [
            GitLogEntry(
                hash=f"hash{i}",
                short_hash=f"h{i}",
                author="Dev",
                date=f"{i}h ago",
                message=f"commit {i}",
            )
            for i in range(3)
        ]
        result = formatter.format_log(entries)
        assert "commit 0" in result
        assert "commit 2" in result

    def test_truncation(self):
        entries = [
            GitLogEntry(
                hash=f"hash{i}",
                short_hash=f"h{i}",
                author="Dev",
                date="now",
                message=f"msg {i}",
            )
            for i in range(20)
        ]
        result = formatter.format_log(entries, max_entries=5)
        assert "msg 4" in result
        assert "msg 5" not in result

    def test_header_present(self):
        entries = [
            GitLogEntry(hash="abc", short_hash="a", author="X", date="now", message="m")
        ]
        result = formatter.format_log(entries)
        assert "Recent commits:" in result


class TestFormatDiff:
    def test_empty_diff(self):
        result = formatter.format_diff("")
        assert result == "No changes to display."

    def test_whitespace_only_diff(self):
        result = formatter.format_diff("   \n  \n  ")
        assert result == "No changes to display."

    def test_short_diff_unchanged(self):
        diff = "--- a/file.py\n+++ b/file.py\n-old\n+new"
        result = formatter.format_diff(diff)
        assert result == diff

    def test_long_diff_truncated(self):
        diff = "line\n" * 5000
        result = formatter.format_diff(diff, max_length=100)
        assert "truncated" in result
        assert len(result) < len(diff)

    def test_truncation_at_newline(self):
        diff = "a" * 50 + "\n" + "b" * 50 + "\n" + "c" * 50
        result = formatter.format_diff(diff, max_length=60)
        assert "truncated" in result
        # Should not end with a partial line before the truncation notice
        lines = result.split("\n")
        assert lines[0] == "a" * 50

    def test_exact_limit_no_truncation(self):
        diff = "short diff"
        result = formatter.format_diff(diff, max_length=3500)
        assert "truncated" not in result
        assert result == diff


class TestFormatResult:
    def test_success_default_emoji(self):
        r = GitResult(success=True, message="Done")
        result = formatter.format_result(r)
        assert "\u2705" in result
        assert "Done" in result

    def test_failure_default_emoji(self):
        r = GitResult(success=False, message="Failed")
        result = formatter.format_result(r)
        assert "\u274c" in result
        assert "Failed" in result

    def test_custom_emoji(self):
        r = GitResult(success=True, message="Pushed")
        result = formatter.format_result(r, emoji="\U0001f680")
        assert "\U0001f680" in result
        assert "\u2705" not in result

    def test_with_details(self):
        r = GitResult(success=True, message="Done", details="extra info")
        result = formatter.format_result(r)
        assert "extra info" in result

    def test_without_details(self):
        r = GitResult(success=True, message="Done")
        result = formatter.format_result(r)
        assert result == "\u2705 Done"


class TestFormatHelp:
    def test_contains_all_commands(self):
        result = formatter.format_help()
        assert "/git" in result
        assert "/git status" in result
        assert "/git branch" in result
        assert "/git checkout" in result
        assert "/git diff" in result
        assert "/git log" in result
        assert "/git add" in result
        assert "/git commit" in result
        assert "/git push" in result
        assert "/git pull" in result
        assert "/git help" in result

    def test_has_header(self):
        result = formatter.format_help()
        assert "Git Commands:" in result


class TestBuildAutoMessage:
    def test_single_modified(self):
        staged = [FileChange(path="src/app.py", status="modified")]
        assert formatter.build_auto_message(staged) == "update src/app.py"

    def test_single_added(self):
        staged = [FileChange(path="new_file.py", status="added")]
        assert formatter.build_auto_message(staged) == "add new_file.py"

    def test_multiple_same_status(self):
        staged = [
            FileChange(path="a.py", status="modified"),
            FileChange(path="b.py", status="modified"),
        ]
        assert formatter.build_auto_message(staged) == "update 2 files"

    def test_multiple_mixed(self):
        staged = [
            FileChange(path="a.py", status="modified"),
            FileChange(path="b.py", status="added"),
        ]
        result = formatter.build_auto_message(staged)
        assert result.startswith("update 2 files (")

    def test_empty(self):
        assert formatter.build_auto_message([]) == "update files"


class TestFormatStatusEdgeCases:
    def test_unicode_paths(self):
        status = GitStatus(
            branch="main",
            staged=[FileChange(path="src/\u4e2d\u6587.py", status="modified")],
            untracked=["\U0001f600_emoji.txt"],
        )
        result = formatter.format_status(status)
        assert "\u4e2d\u6587.py" in result
        assert "\U0001f600_emoji.txt" in result

    def test_very_long_path(self):
        long_path = "a/" * 250 + "file.py"
        status = GitStatus(
            branch="main",
            staged=[FileChange(path=long_path, status="modified")],
        )
        result = formatter.format_status(status)
        assert long_path in result

    def test_many_files(self):
        status = GitStatus(
            branch="main",
            staged=[
                FileChange(path=f"file{i}.py", status="modified") for i in range(100)
            ],
        )
        result = formatter.format_status(status)
        assert "file0.py" in result
        assert "file99.py" in result

    def test_copied_file_indicator(self):
        status = GitStatus(
            branch="main",
            staged=[FileChange(path="copy.py", status="copied")],
        )
        result = formatter.format_status(status)
        assert "C copy.py" in result


class TestBuildAutoMessageEdgeCases:
    def test_conflicted_status_falls_back(self):
        staged = [FileChange(path="conflict.py", status="conflicted")]
        result = formatter.build_auto_message(staged)
        assert result == "update conflict.py"

    def test_mixed_three_statuses_counter(self):
        staged = [
            FileChange(path="a.py", status="added"),
            FileChange(path="b.py", status="added"),
            FileChange(path="c.py", status="added"),
            FileChange(path="d.py", status="modified"),
            FileChange(path="e.py", status="modified"),
            FileChange(path="f.py", status="deleted"),
        ]
        result = formatter.build_auto_message(staged)
        assert "6 files" in result
        assert "3 added" in result
        assert "2 modified" in result
        assert "1 deleted" in result

    def test_very_long_path_in_single_file(self):
        long_path = "a/" * 100 + "file.py"
        staged = [FileChange(path=long_path, status="modified")]
        result = formatter.build_auto_message(staged)
        assert result == f"update {long_path}"


class TestFormatDiffBoundary:
    def test_exactly_at_max_length_no_truncation(self):
        diff = "x" * 100
        result = formatter.format_diff(diff, max_length=100)
        assert "truncated" not in result
        assert result == diff

    def test_max_length_plus_one_truncated(self):
        diff = "x" * 50 + "\n" + "y" * 50 + "\n"
        result = formatter.format_diff(diff, max_length=51)
        assert "truncated" in result


class TestFormatMergeResultEdgeCases:
    def test_conflicts_but_empty_file_list(self):
        result = MergeResult(
            success=False,
            had_conflicts=True,
            conflicted_files=[],
            message="Merge conflicts detected",
        )
        text = formatter.format_merge_result(result)
        assert "\u26a0\ufe0f" in text
        assert "conflicts" in text.lower()

    def test_many_conflicted_files(self):
        files = [f"file{i}.py" for i in range(50)]
        result = MergeResult(
            success=False,
            had_conflicts=True,
            conflicted_files=files,
            message="Merge conflicts detected",
        )
        text = formatter.format_merge_result(result)
        assert "file0.py" in text
        assert "file49.py" in text
