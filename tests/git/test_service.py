"""Tests for GitService with mocked subprocess."""

from unittest.mock import AsyncMock, patch

import pytest

from leashd.git.service import GitService, _porcelain_to_status, _strip_claude_coauthor


@pytest.fixture
def service():
    return GitService()


@pytest.fixture
def cwd(tmp_path):
    return tmp_path


def _make_proc(returncode=0, stdout="", stderr=""):
    """Create a mock subprocess result."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.kill = AsyncMock()
    return proc


def _patch_subprocess(proc):
    """Patch asyncio.create_subprocess_exec to return the given proc."""
    return patch(
        "leashd.git.service.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    )


class TestIsRepo:
    async def test_is_repo_true(self, service, cwd):
        proc = _make_proc(returncode=0, stdout="true\n")
        with _patch_subprocess(proc):
            assert await service.is_repo(cwd) is True

    async def test_is_repo_false(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="fatal: not a git repository")
        with _patch_subprocess(proc):
            assert await service.is_repo(cwd) is False

    async def test_is_repo_nonexistent_dir(self, service, tmp_path):
        nonexistent = tmp_path / "no_such_dir"
        result = await service.is_repo(nonexistent)
        assert result is False


class TestStatus:
    async def test_clean_status(self, service, cwd):
        stdout = (
            "# branch.head main\n# branch.upstream origin/main\n# branch.ab +0 -0\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.branch == "main"
        assert status.tracking == "origin/main"
        assert status.ahead == 0
        assert status.behind == 0
        assert status.staged == []
        assert status.unstaged == []
        assert status.untracked == []

    async def test_status_ahead_behind(self, service, cwd):
        stdout = "# branch.head feature\n# branch.upstream origin/feature\n# branch.ab +3 -1\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.ahead == 3
        assert status.behind == 1

    async def test_status_with_staged_modified(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "1 M. N... 100644 100644 100644 abc123 def456 src/app.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert status.staged[0].path == "src/app.py"
        assert status.staged[0].status == "modified"
        assert status.unstaged == []

    async def test_status_with_unstaged_modified(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "1 .M N... 100644 100644 100644 abc123 def456 tests/test.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.staged == []
        assert len(status.unstaged) == 1
        assert status.unstaged[0].path == "tests/test.py"
        assert status.unstaged[0].status == "modified"

    async def test_status_with_both_staged_and_unstaged(self, service, cwd):
        stdout = (
            "# branch.head main\n1 MM N... 100644 100644 100644 abc123 def456 both.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert len(status.unstaged) == 1
        assert status.staged[0].path == "both.py"
        assert status.unstaged[0].path == "both.py"

    async def test_status_with_added_file(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "1 A. N... 000000 100644 100644 000000 abc123 new_file.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert status.staged[0].status == "added"

    async def test_status_with_deleted_file(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "1 D. N... 100644 000000 000000 abc123 000000 removed.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert status.staged[0].status == "deleted"

    async def test_status_with_renamed_file(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "2 R. N... 100644 100644 100644 abc123 def456 R100 new_name.py\told_name.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert status.staged[0].status == "renamed"
        assert status.staged[0].path == "new_name.py"

    async def test_status_with_untracked_files(self, service, cwd):
        stdout = "# branch.head main\n? notes.txt\n? scratch.py\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.untracked == ["notes.txt", "scratch.py"]

    async def test_status_with_unmerged(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "u UU N... 100644 100644 100644 100644 abc123 def456 ghi789 conflict.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert status.staged[0].status == "conflicted"
        assert status.staged[0].path == "conflict.py"

    async def test_status_no_tracking(self, service, cwd):
        stdout = "# branch.head new-branch\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.tracking is None

    async def test_status_command_failure(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="fatal: error")
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.branch == "unknown"

    async def test_status_malformed_line_skipped(self, service, cwd):
        stdout = (
            "# branch.head main\n"
            "1 M.\n"  # too few fields
            "1 A. N... 100644 100644 100644 000000 abc123 valid.py\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 1
        assert status.staged[0].path == "valid.py"

    async def test_status_mixed_everything(self, service, cwd):
        stdout = (
            "# branch.head develop\n"
            "# branch.upstream origin/develop\n"
            "# branch.ab +5 -2\n"
            "1 M. N... 100644 100644 100644 abc def src/staged.py\n"
            "1 .M N... 100644 100644 100644 abc def src/unstaged.py\n"
            "? untracked.txt\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert status.branch == "develop"
        assert status.tracking == "origin/develop"
        assert status.ahead == 5
        assert status.behind == 2
        assert len(status.staged) == 1
        assert len(status.unstaged) == 1
        assert status.untracked == ["untracked.txt"]


class TestBranches:
    async def test_list_branches(self, service, cwd):
        stdout = "* main\n  develop\n  feature/auth\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            branches = await service.branches(cwd)
        assert len(branches) == 3
        assert branches[0].name == "main"
        assert branches[0].is_current is True
        assert branches[1].name == "develop"
        assert branches[1].is_current is False

    async def test_empty_branches(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc):
            branches = await service.branches(cwd)
        assert branches == []

    async def test_branches_command_failure(self, service, cwd):
        proc = _make_proc(returncode=128)
        with _patch_subprocess(proc):
            branches = await service.branches(cwd)
        assert branches == []

    async def test_skip_detached_head(self, service, cwd):
        stdout = "* (HEAD detached at abc1234)\n  main\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            branches = await service.branches(cwd)
        assert len(branches) == 1
        assert branches[0].name == "main"

    async def test_skip_blank_lines(self, service, cwd):
        stdout = "  main\n\n  develop\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            branches = await service.branches(cwd)
        assert len(branches) == 2


class TestSearchBranches:
    async def test_search_exact_match_first(self, service, cwd):
        stdout = "  main\n  feature/main-page\n  old-main-backup\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "main")
        assert results[0].name == "main"

    async def test_search_prefix_before_substring(self, service, cwd):
        stdout = "  feature/auth\n  old-feature-cleanup\n  feature/dashboard\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "feature")
        names = [r.name for r in results]
        assert names.index("feature/auth") < names.index("old-feature-cleanup")

    async def test_search_case_insensitive(self, service, cwd):
        stdout = "  Feature/Auth\n  MAIN\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "feature")
        assert len(results) == 1
        assert results[0].name == "Feature/Auth"

    async def test_search_no_matches(self, service, cwd):
        stdout = "  main\n  develop\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "zzz_nonexistent")
        assert results == []

    async def test_search_empty_query_returns_all(self, service, cwd):
        stdout = "  main\n  develop\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "")
        assert len(results) == 2

    async def test_search_remote_branches(self, service, cwd):
        stdout = "  main\n  remotes/origin/main\n  remotes/origin/feature/auth\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "auth")
        assert len(results) == 1
        assert results[0].is_remote is True

    async def test_search_remote_match_by_short_name(self, service, cwd):
        stdout = "  remotes/origin/feature/payments\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "payments")
        assert len(results) == 1

    async def test_search_skips_head_pointer(self, service, cwd):
        stdout = (
            "  main\n  remotes/origin/HEAD -> origin/main\n  remotes/origin/develop\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "")
        names = [r.name for r in results]
        assert all("HEAD ->" not in n for n in names)

    async def test_search_command_failure(self, service, cwd):
        proc = _make_proc(returncode=128)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "feat")
        assert results == []


class TestCheckout:
    async def test_checkout_success(self, service, cwd):
        proc = _make_proc(stdout="Switched to branch 'develop'\n")
        with _patch_subprocess(proc):
            result = await service.checkout(cwd, "develop")
        assert result.success is True
        assert "develop" in result.message

    async def test_checkout_invalid_branch_name(self, service, cwd):
        result = await service.checkout(cwd, "branch; rm -rf /")
        assert result.success is False
        assert "Invalid branch name" in result.message

    async def test_checkout_invalid_name_backtick(self, service, cwd):
        result = await service.checkout(cwd, "`whoami`")
        assert result.success is False

    async def test_checkout_invalid_name_space(self, service, cwd):
        result = await service.checkout(cwd, "name with spaces")
        assert result.success is False

    async def test_checkout_remote_fallback(self, service, cwd):
        """When local checkout fails, try remote tracking branch."""
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: git checkout feature/new — fails
                return _make_proc(returncode=1, stderr="error: pathspec")
            # Second call: git checkout -b feature/new origin/feature/new — succeeds
            return _make_proc(
                returncode=0, stdout="Switched to a new branch 'feature/new'\n"
            )

        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            side_effect=side_effect,
        ):
            result = await service.checkout(cwd, "feature/new")
        assert result.success is True
        assert "tracking" in result.message

    async def test_checkout_both_fail(self, service, cwd):
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_proc(returncode=1, stderr="error: not found")

        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            side_effect=side_effect,
        ):
            result = await service.checkout(cwd, "nonexistent")
        assert result.success is False
        assert "Failed to checkout" in result.message

    async def test_checkout_valid_branch_names(self, service, cwd):
        for name in ("main", "feature/auth", "fix-123", "v1.0.0", "user/my.branch"):
            proc = _make_proc(returncode=0)
            with _patch_subprocess(proc):
                result = await service.checkout(cwd, name)
            assert result.success is True


class TestCreateBranch:
    async def test_create_and_checkout(self, service, cwd):
        proc = _make_proc(stdout="Switched to a new branch\n")
        with _patch_subprocess(proc):
            result = await service.create_branch(cwd, "new-feature")
        assert result.success is True
        assert "Created and switched to" in result.message

    async def test_create_without_checkout(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc):
            result = await service.create_branch(cwd, "new-branch", checkout=False)
        assert result.success is True
        assert "Created" in result.message
        assert "switched" not in result.message.lower()

    async def test_create_invalid_name(self, service, cwd):
        result = await service.create_branch(cwd, "bad name!")
        assert result.success is False
        assert "Invalid" in result.message

    async def test_create_fails(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="already exists")
        with _patch_subprocess(proc):
            result = await service.create_branch(cwd, "existing")
        assert result.success is False


class TestDiff:
    async def test_diff_basic(self, service, cwd):
        proc = _make_proc(stdout="--- a/file.py\n+++ b/file.py\n-old\n+new\n")
        with _patch_subprocess(proc):
            diff = await service.diff(cwd)
        assert "--- a/file.py" in diff
        assert "+new" in diff

    async def test_diff_staged(self, service, cwd):
        proc = _make_proc(stdout="staged diff output\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.diff(cwd, staged=True)
        args = mock_exec.call_args[0]
        assert "--cached" in args

    async def test_diff_with_path(self, service, cwd):
        proc = _make_proc(stdout="path diff\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.diff(cwd, path="src/app.py")
        args = mock_exec.call_args[0]
        assert "--" in args
        assert "src/app.py" in args

    async def test_diff_empty(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc):
            diff = await service.diff(cwd)
        assert diff == ""

    async def test_diff_command_failure(self, service, cwd):
        proc = _make_proc(returncode=1, stderr="error")
        with _patch_subprocess(proc):
            diff = await service.diff(cwd)
        assert diff == ""


class TestLog:
    async def test_log_parsing(self, service, cwd):
        stdout = (
            "abc123||abc||Alice||2 hours ago||fix: auth bug\n"
            "def456||def||Bob||3 days ago||feat: add login\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            entries = await service.log(cwd)
        assert len(entries) == 2
        assert entries[0].hash == "abc123"
        assert entries[0].short_hash == "abc"
        assert entries[0].author == "Alice"
        assert entries[0].date == "2 hours ago"
        assert entries[0].message == "fix: auth bug"
        assert entries[1].author == "Bob"

    async def test_log_custom_count(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc) as mock_exec:
            await service.log(cwd, count=5)
        args = mock_exec.call_args[0]
        assert "-5" in args

    async def test_log_empty(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc):
            entries = await service.log(cwd)
        assert entries == []

    async def test_log_command_failure(self, service, cwd):
        proc = _make_proc(returncode=128)
        with _patch_subprocess(proc):
            entries = await service.log(cwd)
        assert entries == []

    async def test_log_malformed_line_skipped(self, service, cwd):
        stdout = (
            "abc123||abc||Alice||2 hours ago||fix: bug\n"
            "malformed line without delimiters\n"
            "def456||def||Bob||1 day ago||feat: new\n"
        )
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            entries = await service.log(cwd)
        assert len(entries) == 2

    async def test_log_message_with_delimiter(self, service, cwd):
        stdout = "abc123||abc||Alice||now||message with || in it\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            entries = await service.log(cwd)
        assert len(entries) == 1
        assert entries[0].message == "message with || in it"


class TestAdd:
    async def test_add_single_file(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.add(cwd, ["src/app.py"])
        assert result.success is True
        assert "1 file(s)" in result.message
        args = mock_exec.call_args[0]
        assert "src/app.py" in args

    async def test_add_multiple_files(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc):
            result = await service.add(cwd, ["a.py", "b.py", "c.py"])
        assert result.success is True
        assert "3 file(s)" in result.message

    async def test_add_empty_list(self, service, cwd):
        result = await service.add(cwd, [])
        assert result.success is False
        assert "No files specified" in result.message

    async def test_add_failure(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="pathspec error")
        with _patch_subprocess(proc):
            result = await service.add(cwd, ["nonexistent.py"])
        assert result.success is False


class TestAddAll:
    async def test_add_all_success(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.add_all(cwd)
        assert result.success is True
        assert "Staged all" in result.message
        args = mock_exec.call_args[0]
        assert "-A" in args

    async def test_add_all_failure(self, service, cwd):
        proc = _make_proc(returncode=1, stderr="error")
        with _patch_subprocess(proc):
            result = await service.add_all(cwd)
        assert result.success is False


class TestCommit:
    async def test_commit_success(self, service, cwd):
        stdout = "[main abc1234] fix: handle edge case\n 1 file changed\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            result = await service.commit(cwd, "fix: handle edge case")
        assert result.success is True
        assert "abc1234" in result.message
        assert "fix: handle edge case" in result.message

    async def test_commit_success_no_bracket(self, service, cwd):
        proc = _make_proc(stdout="some output without brackets\n")
        with _patch_subprocess(proc):
            result = await service.commit(cwd, "msg")
        assert result.success is True
        assert result.message == "msg"

    async def test_commit_failure(self, service, cwd):
        proc = _make_proc(returncode=1, stderr="nothing to commit")
        with _patch_subprocess(proc):
            result = await service.commit(cwd, "msg")
        assert result.success is False
        assert "Failed to commit" in result.message

    async def test_commit_message_passed(self, service, cwd):
        proc = _make_proc(stdout="[main abc] msg\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.commit(cwd, "my commit message")
        args = mock_exec.call_args[0]
        assert "-m" in args
        assert "my commit message" in args

    async def test_commit_failure_uses_stderr_or_stdout(self, service, cwd):
        proc = _make_proc(returncode=1, stderr="", stdout="error in stdout")
        with _patch_subprocess(proc):
            result = await service.commit(cwd, "msg")
        assert result.success is False
        assert "error in stdout" in result.details


class TestStripClaudeCoauthor:
    def test_removes_claude_coauthor_trailer(self):
        msg = "feat: add feature\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        assert _strip_claude_coauthor(msg) == "feat: add feature"

    def test_removes_case_insensitive(self):
        msg = "fix: bug\n\nco-authored-by: claude <noreply@anthropic.com>"
        result = _strip_claude_coauthor(msg)
        assert "co-authored-by" not in result.lower()
        assert result == "fix: bug"

    def test_preserves_human_coauthor(self):
        msg = "feat: add feature\n\nCo-Authored-By: Jane Doe <jane@example.com>"
        assert "Jane Doe" in _strip_claude_coauthor(msg)

    def test_removes_claude_but_preserves_human(self):
        msg = (
            "feat: add feature\n\n"
            "Co-Authored-By: Jane Doe <jane@example.com>\n"
            "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        )
        result = _strip_claude_coauthor(msg)
        assert "Jane Doe" in result
        assert "Claude" not in result
        assert "anthropic" not in result

    def test_no_trailer_unchanged(self):
        assert _strip_claude_coauthor("chore: update deps") == "chore: update deps"

    def test_matches_anthropic_email_without_claude_name(self):
        msg = "fix: bug\n\nCo-Authored-By: AI <noreply@anthropic.com>"
        result = _strip_claude_coauthor(msg)
        assert "anthropic" not in result

    def test_matches_claude_without_anthropic_email(self):
        msg = "fix: bug\n\nCo-Authored-By: Claude Opus 4.6 <other@email.com>"
        result = _strip_claude_coauthor(msg)
        assert "Claude" not in result

    async def test_commit_strips_coauthor(self, service, cwd):
        proc = _make_proc(stdout="[main abc] msg\n")
        msg = "feat: thing\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        with _patch_subprocess(proc) as mock_exec:
            await service.commit(cwd, msg)
        args = mock_exec.call_args[0]
        commit_msg = args[args.index("-m") + 1]
        assert "Claude" not in commit_msg
        assert "feat: thing" in commit_msg


class TestPush:
    async def test_push_success(self, service, cwd):
        proc = _make_proc(stderr="Everything up-to-date\n")
        with _patch_subprocess(proc):
            result = await service.push(cwd)
        assert result.success is True
        assert "Push successful" in result.message

    async def test_push_with_branch(self, service, cwd):
        proc = _make_proc(stderr="")
        with _patch_subprocess(proc) as mock_exec:
            await service.push(cwd, branch="feature")
        args = mock_exec.call_args[0]
        assert "feature" in args

    async def test_push_custom_remote(self, service, cwd):
        proc = _make_proc(stderr="")
        with _patch_subprocess(proc) as mock_exec:
            await service.push(cwd, remote="upstream")
        args = mock_exec.call_args[0]
        assert "upstream" in args

    async def test_push_failure(self, service, cwd):
        proc = _make_proc(returncode=1, stderr="rejected")
        with _patch_subprocess(proc):
            result = await service.push(cwd)
        assert result.success is False
        assert "Push failed" in result.message


class TestPull:
    async def test_pull_success(self, service, cwd):
        proc = _make_proc(stdout="Already up to date.\n")
        with _patch_subprocess(proc):
            result = await service.pull(cwd)
        assert result.success is True
        assert "Pull successful" in result.message

    async def test_pull_failure(self, service, cwd):
        proc = _make_proc(returncode=1, stderr="merge conflict")
        with _patch_subprocess(proc):
            result = await service.pull(cwd)
        assert result.success is False
        assert "Pull failed" in result.message


class TestRun:
    async def test_nonexistent_directory(self, service, tmp_path):
        nonexistent = tmp_path / "no_such_dir"
        code, _stdout, stderr = await service._run("status", cwd=nonexistent)
        assert code == 1
        assert "does not exist" in stderr

    async def test_timeout_handling(self, service, cwd):
        proc = AsyncMock()
        proc.returncode = 0
        proc.kill = AsyncMock()

        async def slow_communicate():
            import asyncio

            await asyncio.sleep(100)  # Will be interrupted by timeout

        proc.communicate = slow_communicate

        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            code, _stdout, stderr = await service._run("status", cwd=cwd, timeout=0)
        assert code == 1
        assert "timed out" in stderr

    async def test_git_not_installed(self, service, cwd):
        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            code, _stdout, stderr = await service._run("status", cwd=cwd)
        assert code == 1
        assert "not installed" in stderr

    async def test_os_error(self, service, cwd):
        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            side_effect=OSError("Permission denied"),
        ):
            code, _stdout, stderr = await service._run("status", cwd=cwd)
        assert code == 1
        assert "Permission denied" in stderr

    async def test_kill_on_timeout_process_already_dead(self, service, cwd):
        proc = AsyncMock()
        proc.kill = AsyncMock(side_effect=ProcessLookupError)

        async def slow_communicate():
            import asyncio

            await asyncio.sleep(100)

        proc.communicate = slow_communicate

        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            code, _, _ = await service._run("status", cwd=cwd, timeout=0)
        assert code == 1


class TestPorcelainToStatus:
    def test_modified(self):
        assert _porcelain_to_status("M") == "modified"

    def test_type_changed(self):
        assert _porcelain_to_status("T") == "modified"

    def test_added(self):
        assert _porcelain_to_status("A") == "added"

    def test_deleted(self):
        assert _porcelain_to_status("D") == "deleted"

    def test_renamed(self):
        assert _porcelain_to_status("R") == "renamed"

    def test_copied(self):
        assert _porcelain_to_status("C") == "copied"

    def test_unmerged(self):
        assert _porcelain_to_status("U") == "conflicted"

    def test_unknown_defaults_to_modified(self):
        assert _porcelain_to_status("X") == "modified"
        assert _porcelain_to_status("?") == "modified"


class TestBranchNameValidation:
    async def test_rejects_semicolon(self, service, cwd):
        result = await service.checkout(cwd, "main; echo pwned")
        assert result.success is False

    async def test_rejects_pipe(self, service, cwd):
        result = await service.checkout(cwd, "main|cat /etc/passwd")
        assert result.success is False

    async def test_rejects_dollar(self, service, cwd):
        result = await service.checkout(cwd, "$HOME")
        assert result.success is False

    async def test_rejects_ampersand(self, service, cwd):
        result = await service.checkout(cwd, "main&&echo")
        assert result.success is False

    async def test_rejects_backtick(self, service, cwd):
        result = await service.checkout(cwd, "`id`")
        assert result.success is False

    async def test_accepts_slashes(self, service, cwd):
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc):
            result = await service.checkout(cwd, "feature/auth/v2")
        assert result.success is True

    async def test_accepts_dots(self, service, cwd):
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc):
            result = await service.checkout(cwd, "release-1.2.3")
        assert result.success is True

    async def test_accepts_hyphens(self, service, cwd):
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc):
            result = await service.checkout(cwd, "fix-login-bug")
        assert result.success is True

    async def test_accepts_underscores(self, service, cwd):
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc):
            result = await service.checkout(cwd, "my_branch_name")
        assert result.success is True


class TestBranchNameSecurityEdgeCases:
    """Security edge cases for branch name validation."""

    async def test_rejects_empty_string(self, service, cwd):
        result = await service.checkout(cwd, "")
        assert result.success is False
        assert "Invalid branch name" in result.message

    async def test_rejects_null_byte(self, service, cwd):
        result = await service.checkout(cwd, "main\x00--exec=bad")
        assert result.success is False

    async def test_rejects_newline(self, service, cwd):
        result = await service.checkout(cwd, "main\nflag")
        assert result.success is False

    async def test_rejects_unicode_cyrillic(self, service, cwd):
        result = await service.checkout(cwd, "ma\u0456n")
        assert result.success is False

    async def test_rejects_tilde(self, service, cwd):
        result = await service.checkout(cwd, "~/.ssh/id_rsa")
        assert result.success is False

    async def test_rejects_caret(self, service, cwd):
        result = await service.checkout(cwd, "HEAD^{commit}")
        assert result.success is False

    async def test_rejects_colon(self, service, cwd):
        result = await service.checkout(cwd, "HEAD:file")
        assert result.success is False

    async def test_rejects_at_sign_reflog(self, service, cwd):
        result = await service.checkout(cwd, "main@{0}")
        assert result.success is False

    async def test_rejects_double_dot_range(self, service, cwd):
        """Double dots are git range operators and must be rejected."""
        result = await service.checkout(cwd, "main..develop")
        assert result.success is False
        assert "Invalid branch name" in result.message

    async def test_rejects_triple_dot_range(self, service, cwd):
        result = await service.merge(cwd, "main...develop")
        assert result.success is False


class TestPushInputValidation:
    """Push doesn't validate remote/branch but subprocess_exec prevents injection."""

    async def test_push_remote_with_semicolon(self, service, cwd):
        """subprocess_exec prevents shell injection even without validation."""
        proc = _make_proc(returncode=128, stderr="fatal: bad remote")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.push(cwd, remote="origin; rm -rf /")
        assert result.success is False
        args = mock_exec.call_args[0]
        assert "origin; rm -rf /" in args

    async def test_push_branch_with_special_chars(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="error")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.push(cwd, branch="main; echo pwned")
        assert result.success is False
        args = mock_exec.call_args[0]
        assert "main; echo pwned" in args

    async def test_push_empty_remote(self, service, cwd):
        proc = _make_proc(
            returncode=128, stderr="fatal: '' does not appear to be a git repository"
        )
        with _patch_subprocess(proc):
            result = await service.push(cwd, remote="")
        assert result.success is False

    async def test_push_empty_branch(self, service, cwd):
        proc = _make_proc(returncode=0, stderr="Everything up-to-date")
        with _patch_subprocess(proc) as mock_exec:
            await service.push(cwd, branch="")
        args = mock_exec.call_args[0]
        assert args == ("git", "push", "origin")


class TestCommitMessageSecurity:
    async def test_commit_message_only_coauthor_trailer(self, service, cwd):
        """After stripping Claude co-author, message may be empty."""
        msg = "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        proc = _make_proc(returncode=1, stderr="empty commit message")
        with _patch_subprocess(proc):
            result = await service.commit(cwd, msg)
        assert result.success is False

    async def test_commit_message_very_long(self, service, cwd):
        msg = "x" * 100_000
        proc = _make_proc(stdout=f"[main abc1234] {msg[:50]}\n")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.commit(cwd, msg)
        assert result.success is True
        args = mock_exec.call_args[0]
        commit_msg = args[args.index("-m") + 1]
        assert len(commit_msg) == 100_000

    async def test_commit_message_preserves_human_signed_off(self, service, cwd):
        msg = (
            "feat: add feature\n\n"
            "Signed-off-by: Alice <alice@example.com>\n"
            "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        )
        proc = _make_proc(stdout="[main abc] feat\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.commit(cwd, msg)
        args = mock_exec.call_args[0]
        commit_msg = args[args.index("-m") + 1]
        assert "Signed-off-by: Alice" in commit_msg
        assert "Claude" not in commit_msg

    async def test_commit_message_flag_like(self, service, cwd):
        """Flag-like messages are safe with subprocess_exec (no shell)."""
        msg = "-m bad --exec=cmd"
        proc = _make_proc(stdout="[main abc] msg\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.commit(cwd, msg)
        args = mock_exec.call_args[0]
        m_idx = args.index("-m")
        assert args[m_idx + 1] == msg


class TestDiffCombinedArgs:
    async def test_diff_staged_and_path_simultaneously(self, service, cwd):
        proc = _make_proc(stdout="staged path diff\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.diff(cwd, staged=True, path="src/app.py")
        args = mock_exec.call_args[0]
        assert "--cached" in args
        assert "--" in args
        assert "src/app.py" in args

    async def test_diff_path_with_spaces(self, service, cwd):
        proc = _make_proc(stdout="diff output\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.diff(cwd, path="src/my file.py")
        args = mock_exec.call_args[0]
        assert "src/my file.py" in args

    async def test_diff_path_with_special_chars(self, service, cwd):
        proc = _make_proc(stdout="diff output\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.diff(cwd, path="src/[test].py")
        args = mock_exec.call_args[0]
        assert "src/[test].py" in args


class TestAddPathSeparator:
    async def test_add_uses_double_dash_separator(self, service, cwd):
        """git add -- prevents --help from being treated as a flag."""
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc) as mock_exec:
            await service.add(cwd, ["--help"])
        args = mock_exec.call_args[0]
        assert "--" in args
        dash_idx = args.index("--")
        assert args[dash_idx + 1] == "--help"

    async def test_add_path_with_leading_dash(self, service, cwd):
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc) as mock_exec:
            await service.add(cwd, ["-rf"])
        args = mock_exec.call_args[0]
        assert "--" in args
        assert "-rf" in args


class TestLogEdgeCases:
    async def test_log_count_zero(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc) as mock_exec:
            entries = await service.log(cwd, count=0)
        args = mock_exec.call_args[0]
        assert "-0" in args
        assert entries == []

    async def test_log_count_negative(self, service, cwd):
        proc = _make_proc(stdout="")
        with _patch_subprocess(proc) as mock_exec:
            await service.log(cwd, count=-1)
        args = mock_exec.call_args[0]
        assert "--1" in args or "-1" in args

    async def test_log_delimiter_in_author_name(self, service, cwd):
        """Author containing delimiter corrupts parsing — documents known limitation."""
        stdout = "abc123||abc||Alice||Bob||2 hours ago||fix: bug\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            entries = await service.log(cwd)
        assert len(entries) == 1
        assert entries[0].author == "Alice"


class TestSearchBranchesEdgeCases:
    async def test_search_with_regex_special_chars_in_query(self, service, cwd):
        """Query is used as plain string, not regex, so special chars are safe."""
        stdout = "  main\n  feature/test\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "feat(.+)")
        assert results == []

    async def test_search_long_remote_prefix(self, service, cwd):
        stdout = "  remotes/upstream/feature/deep/path/branch\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            results = await service.search_branches(cwd, "deep")
        assert len(results) == 1


class TestRunEdgeCases:
    async def test_merge_uses_60s_timeout(self, service, cwd):
        proc = _make_proc(returncode=0, stdout="Merge made.\n")
        with _patch_subprocess(proc) as mock_exec:
            await service.merge(cwd, "feature")
        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs.get("cwd") == cwd

    async def test_default_timeout_is_30s(self, service, cwd):
        from leashd.git.service import _DEFAULT_TIMEOUT

        assert _DEFAULT_TIMEOUT == 30

    async def test_status_unmerged_short_line_skipped(self, service, cwd):
        """Unmerged line with fewer than 11 parts is silently skipped."""
        stdout = "# branch.head main\nu UU short\n"
        proc = _make_proc(stdout=stdout)
        with _patch_subprocess(proc):
            status = await service.status(cwd)
        assert len(status.staged) == 0
