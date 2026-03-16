"""Tests for the web checkpoint module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.plugins.builtin.web_checkpoint import (
    DraftedComment,
    PostedComment,
    ScannedPost,
    WebCheckpoint,
    checkpoint_to_markdown,
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
)


def _make_checkpoint(**overrides) -> WebCheckpoint:
    defaults = {
        "session_id": "test-123",
        "created_at": "2026-03-16T10:00:00Z",
        "updated_at": "2026-03-16T10:05:00Z",
    }
    defaults.update(overrides)
    return WebCheckpoint(**defaults)


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path):
        cp = _make_checkpoint(
            platform="LinkedIn",
            recipe_name="linkedin_comment",
            topic="AI safety",
        )
        save_checkpoint(str(tmp_path), cp)
        loaded = load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded.session_id == "test-123"
        assert loaded.platform == "LinkedIn"
        assert loaded.topic == "AI safety"

    def test_load_missing_returns_none(self, tmp_path):
        assert load_checkpoint(str(tmp_path)) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text("not json{{{")
        assert load_checkpoint(str(tmp_path)) is None

    def test_load_corrupt_logs_warning(self, tmp_path, capsys):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text("not json{{{")
        load_checkpoint(str(tmp_path))
        captured = capsys.readouterr()
        assert "checkpoint_corrupt" in captured.out

    def test_load_invalid_schema_returns_none(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text(
            json.dumps({"bad": "schema"})
        )
        assert load_checkpoint(str(tmp_path)) is None

    def test_load_tolerates_extra_fields(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        data = {
            "session_id": "s1",
            "created_at": "2026-03-16T10:00:00Z",
            "updated_at": "2026-03-16T10:05:00Z",
            "unknown_future_field": "some value",
        }
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text(json.dumps(data))
        loaded = load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded.session_id == "s1"

    def test_load_missing_required_fields_returns_none(self, tmp_path):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text(
            json.dumps({"platform": "LinkedIn"})
        )
        assert load_checkpoint(str(tmp_path)) is None

    def test_load_oserror_returns_none_and_logs(self, tmp_path, capsys):
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text("placeholder")
        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            result = load_checkpoint(str(tmp_path))
        assert result is None
        captured = capsys.readouterr()
        assert "checkpoint_read_error" in captured.out


class TestClearCheckpoint:
    def test_clear_removes_file(self, tmp_path):
        cp = _make_checkpoint()
        save_checkpoint(str(tmp_path), cp)
        path = tmp_path / ".leashd" / "web-checkpoint.json"
        assert path.is_file()
        clear_checkpoint(str(tmp_path))
        assert not path.is_file()

    def test_clear_nonexistent_no_error(self, tmp_path):
        clear_checkpoint(str(tmp_path))


class TestAtomicWrite:
    def test_no_tmp_file_left(self, tmp_path):
        cp = _make_checkpoint()
        save_checkpoint(str(tmp_path), cp)
        tmp_file = tmp_path / ".leashd" / "web-checkpoint.json.tmp"
        assert not tmp_file.exists()

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "nested"
        cp = _make_checkpoint()
        save_checkpoint(str(target), cp)
        assert (target / ".leashd" / "web-checkpoint.json").is_file()


class TestCheckpointToMarkdown:
    def test_basic(self):
        cp = _make_checkpoint(platform="LinkedIn", auth_status="authenticated")
        md = checkpoint_to_markdown(cp)
        assert "# Web Session — LinkedIn" in md
        assert "**Auth:** authenticated" in md
        assert "**Session ID:** test-123" in md

    def test_with_posts(self):
        posts = [
            ScannedPost(index=0, author="Alice", snippet="Great post about AI"),
            ScannedPost(index=1, author="Bob", snippet="ML trends"),
        ]
        cp = _make_checkpoint(posts_scanned=posts)
        md = checkpoint_to_markdown(cp)
        assert "Posts Scanned (2)" in md
        assert "Alice" in md
        assert "Bob" in md
        assert "Great post about AI" in md

    def test_with_comments(self):
        post = ScannedPost(index=0, author="Alice")
        drafted = [
            DraftedComment(
                target_post=post,
                draft_text="Nice insights!",
                status="approved",
                approved_text="Nice insights!",
            )
        ]
        posted = [
            PostedComment(
                target_post=post,
                comment_text="Nice insights!",
                posted_at="2026-03-16T10:10:00Z",
            )
        ]
        cp = _make_checkpoint(comments_drafted=drafted, comments_posted=posted)
        md = checkpoint_to_markdown(cp)
        assert "Comments Drafted (1)" in md
        assert "Comments Posted (1)" in md
        assert "Alice" in md
        assert "Nice insights!" in md

    def test_with_pending_actions(self):
        cp = _make_checkpoint(pending_actions=["scroll down", "scan more posts"])
        md = checkpoint_to_markdown(cp)
        assert "Pending Actions" in md
        assert "scroll down" in md

    def test_with_error(self):
        cp = _make_checkpoint(last_error="Rate limited by LinkedIn")
        md = checkpoint_to_markdown(cp)
        assert "Last Error" in md
        assert "Rate limited" in md

    def test_with_phase(self):
        cp = _make_checkpoint(current_phase="comment_on_post", current_step_index=3)
        md = checkpoint_to_markdown(cp)
        assert "**Phase:** comment_on_post (step 3)" in md

    def test_with_topic(self):
        cp = _make_checkpoint(topic="agentic coding")
        md = checkpoint_to_markdown(cp)
        assert "**Topic:** agentic coding" in md

    def test_empty_progress_summary_omits_section(self):
        cp = _make_checkpoint(progress_summary="")
        md = checkpoint_to_markdown(cp)
        assert "## Progress" not in md

    def test_empty_pending_work_omits_section(self):
        cp = _make_checkpoint(pending_work="")
        md = checkpoint_to_markdown(cp)
        assert "## Pending Work" not in md

    def test_with_recipe_name(self):
        cp = _make_checkpoint(recipe_name="linkedin_comment")
        md = checkpoint_to_markdown(cp)
        assert "**Recipe:** linkedin_comment" in md

    def test_with_auth_user(self):
        cp = _make_checkpoint(auth_user="john_doe")
        md = checkpoint_to_markdown(cp)
        assert "**User:** john_doe" in md

    def test_with_current_url(self):
        cp = _make_checkpoint(current_url="https://linkedin.com/feed")
        md = checkpoint_to_markdown(cp)
        assert "**URL:** https://linkedin.com/feed" in md

    def test_with_task_description(self):
        cp = _make_checkpoint(task_description="Comment on AI posts")
        md = checkpoint_to_markdown(cp)
        assert "**Task:** Comment on AI posts" in md

    def test_all_sections_populated(self):
        post = ScannedPost(index=0, author="Alice", snippet="AI post")
        cp = _make_checkpoint(
            platform="LinkedIn",
            recipe_name="linkedin_comment",
            auth_status="authenticated",
            auth_user="john_doe",
            current_url="https://linkedin.com/feed",
            current_phase="comment_on_post",
            current_step_index=2,
            topic="AI safety",
            task_description="Comment on posts",
            progress_summary="Scanned 3 posts",
            pending_work="Draft comment on Alice's post",
            posts_scanned=[post],
            comments_drafted=[
                DraftedComment(
                    target_post=post, draft_text="Great insights!", status="approved"
                )
            ],
            comments_posted=[
                PostedComment(
                    target_post=post,
                    comment_text="Great insights!",
                    posted_at="2026-03-16T10:10:00Z",
                )
            ],
            pending_actions=["scroll down"],
            last_error="Rate limited",
        )
        md = checkpoint_to_markdown(cp)
        assert "# Web Session — LinkedIn" in md
        assert "## Progress" in md
        assert "## Pending Work" in md
        assert "## Posts Scanned (1)" in md
        assert "## Comments Drafted (1)" in md
        assert "## Comments Posted (1)" in md
        assert "## Pending Actions" in md
        assert "## Last Error" in md


class TestCheckpointModels:
    def test_scanned_post_frozen(self):
        from pydantic import ValidationError

        p = ScannedPost(index=0, author="Alice")
        with pytest.raises(ValidationError, match="frozen"):
            p.author = "Bob"  # type: ignore[misc]

    def test_checkpoint_frozen(self):
        from pydantic import ValidationError

        cp = _make_checkpoint()
        with pytest.raises(ValidationError, match="frozen"):
            cp.platform = "Twitter"  # type: ignore[misc]

    def test_drafted_comment_default_status(self):
        post = ScannedPost(index=0, author="Alice")
        c = DraftedComment(target_post=post, draft_text="hello")
        assert c.status == "drafted"

    def test_drafted_comment_rejects_invalid_status(self):
        from pydantic import ValidationError

        post = ScannedPost(index=0, author="Alice")
        with pytest.raises(ValidationError):
            DraftedComment(target_post=post, draft_text="hello", status="invalid")  # type: ignore[arg-type]

    def test_checkpoint_defaults(self):
        cp = _make_checkpoint()
        assert cp.platform == ""
        assert cp.browser_backend == "playwright"
        assert cp.auth_status == "unknown"
        assert cp.posts_scanned == []
        assert cp.progress_summary == ""
        assert cp.pending_work == ""
        assert cp.retry_count == 0

    def test_drafted_comment_accepts_all_valid_statuses(self):
        post = ScannedPost(index=0, author="Alice")
        for status in ("drafted", "approved", "rejected", "posted"):
            c = DraftedComment(target_post=post, draft_text="hi", status=status)
            assert c.status == status

    def test_default_factory_list_independence(self):
        cp1 = _make_checkpoint()
        cp2 = _make_checkpoint()
        assert cp1.posts_scanned is not cp2.posts_scanned
        assert cp1.comments_drafted is not cp2.comments_drafted
        assert cp1.comments_posted is not cp2.comments_posted
        assert cp1.pending_actions is not cp2.pending_actions

    def test_drafted_comment_frozen(self):
        from pydantic import ValidationError

        post = ScannedPost(index=0, author="Alice")
        c = DraftedComment(target_post=post, draft_text="hello")
        with pytest.raises(ValidationError, match="frozen"):
            c.draft_text = "changed"  # type: ignore[misc]

    def test_posted_comment_frozen(self):
        from pydantic import ValidationError

        post = ScannedPost(index=0, author="Alice")
        pc = PostedComment(
            target_post=post, comment_text="hello", posted_at="2026-03-16T10:00:00Z"
        )
        with pytest.raises(ValidationError, match="frozen"):
            pc.comment_text = "changed"  # type: ignore[misc]

    def test_scanned_post_url_optional(self):
        p_none = ScannedPost(index=0, author="Alice")
        assert p_none.url is None
        p_set = ScannedPost(index=0, author="Alice", url="https://x.com/1")
        assert p_set.url == "https://x.com/1"

    def test_scanned_post_snippet_defaults_empty(self):
        p = ScannedPost(index=0, author="Alice")
        assert p.snippet == ""

    def test_checkpoint_to_markdown_with_summary(self):
        cp = _make_checkpoint(
            progress_summary="Scanned 5 posts, drafted 2 comments",
            pending_work="Post approved comment on Alice's article",
        )
        md = checkpoint_to_markdown(cp)
        assert "## Progress" in md
        assert "Scanned 5 posts, drafted 2 comments" in md
        assert "## Pending Work" in md
        assert "Post approved comment on Alice's article" in md


class TestCheckpointUnicode:
    def test_unicode_roundtrip_emoji_and_cjk(self, tmp_path):
        post = ScannedPost(
            index=0, author="\u4e16\u754c User", snippet="\U0001f600 great"
        )
        cp = _make_checkpoint(
            platform="\U0001f600 Social",
            progress_summary="Scanned \u4e16\u754c posts",
            pending_work="\U0001f525 more work",
            posts_scanned=[post],
            comments_drafted=[
                DraftedComment(
                    target_post=post, draft_text="\u4e16\u754c \U0001f600 text"
                )
            ],
        )
        save_checkpoint(str(tmp_path), cp)
        loaded = load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded.platform == "\U0001f600 Social"
        assert loaded.progress_summary == "Scanned \u4e16\u754c posts"
        assert loaded.pending_work == "\U0001f525 more work"
        assert loaded.posts_scanned[0].author == "\u4e16\u754c User"
        assert loaded.comments_drafted[0].draft_text == "\u4e16\u754c \U0001f600 text"

    def test_unicode_in_markdown_rendering(self):
        post = ScannedPost(
            index=0, author="\u4e16\u754c User", snippet="\U0001f600 great"
        )
        cp = _make_checkpoint(
            progress_summary="Scanned \u4e16\u754c posts",
            posts_scanned=[post],
        )
        md = checkpoint_to_markdown(cp)
        assert "\u4e16\u754c User" in md
        assert "\U0001f600 great" in md
        assert "Scanned \u4e16\u754c posts" in md


class TestCheckpointLargePayloads:
    def test_many_posts_and_comments_roundtrip(self, tmp_path):
        posts = [
            ScannedPost(index=i, author=f"Author-{i}", snippet=f"text-{i}")
            for i in range(50)
        ]
        drafted = [
            DraftedComment(target_post=posts[i], draft_text=f"draft-{i}")
            for i in range(20)
        ]
        posted = [
            PostedComment(
                target_post=posts[i],
                comment_text=f"posted-{i}",
                posted_at=f"2026-03-16T10:{i:02d}:00Z",
            )
            for i in range(10)
        ]
        cp = _make_checkpoint(
            posts_scanned=posts,
            comments_drafted=drafted,
            comments_posted=posted,
            pending_actions=[f"action-{i}" for i in range(30)],
        )
        save_checkpoint(str(tmp_path), cp)
        loaded = load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert len(loaded.posts_scanned) == 50
        assert len(loaded.comments_drafted) == 20
        assert len(loaded.comments_posted) == 10
        assert len(loaded.pending_actions) == 30

    def test_draft_and_comment_text_truncated_at_80_chars(self):
        post = ScannedPost(index=0, author="Alice")
        cp = _make_checkpoint(
            comments_drafted=[DraftedComment(target_post=post, draft_text="A" * 100)],
            comments_posted=[
                PostedComment(
                    target_post=post,
                    comment_text="B" * 100,
                    posted_at="2026-03-16T10:00:00Z",
                )
            ],
        )
        md = checkpoint_to_markdown(cp)
        assert "A" * 80 in md
        assert "A" * 81 not in md
        assert "B" * 80 in md
        assert "B" * 81 not in md


class TestStringTargetPostCoercion:
    def test_drafted_comment_string_target_post(self):
        dc = DraftedComment.model_validate(
            {"target_post": "Alice Smith", "draft_text": "Great post!"}
        )
        assert isinstance(dc.target_post, ScannedPost)
        assert dc.target_post.author == "Alice Smith"
        assert dc.target_post.index == 0

    def test_posted_comment_string_target_post(self):
        pc = PostedComment.model_validate(
            {
                "target_post": "Bob Jones",
                "comment_text": "Insightful!",
                "posted_at": "2026-03-16T10:00:00Z",
            }
        )
        assert isinstance(pc.target_post, ScannedPost)
        assert pc.target_post.author == "Bob Jones"
        assert pc.target_post.index == 0

    def test_drafted_comment_dict_target_post_unchanged(self):
        dc = DraftedComment(
            target_post=ScannedPost(index=3, author="Alice"),
            draft_text="hello",
        )
        assert dc.target_post.index == 3
        assert dc.target_post.author == "Alice"

    def test_full_checkpoint_load_with_string_target_posts(self, tmp_path):
        data = {
            "session_id": "s1",
            "created_at": "2026-03-16T10:00:00Z",
            "updated_at": "2026-03-16T10:05:00Z",
            "comments_drafted": [
                {"target_post": "Alice", "draft_text": "Nice!", "status": "approved"}
            ],
            "comments_posted": [
                {
                    "target_post": "Bob",
                    "comment_text": "Great!",
                    "posted_at": "2026-03-16T10:10:00Z",
                }
            ],
        }
        (tmp_path / ".leashd").mkdir()
        (tmp_path / ".leashd" / "web-checkpoint.json").write_text(json.dumps(data))
        loaded = load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded.comments_drafted[0].target_post.author == "Alice"
        assert loaded.comments_posted[0].target_post.author == "Bob"


class TestCommentPhase:
    def test_comment_phase_roundtrip(self, tmp_path):
        cp = _make_checkpoint(comment_phase="typed")
        save_checkpoint(str(tmp_path), cp)
        loaded = load_checkpoint(str(tmp_path))
        assert loaded is not None
        assert loaded.comment_phase == "typed"

    def test_comment_phase_none_by_default(self):
        cp = _make_checkpoint()
        assert cp.comment_phase is None

    def test_comment_phase_all_valid_values(self):
        for phase in (
            "selected",
            "drafting",
            "approved",
            "typing",
            "typed",
            "submitting",
            "submitted",
            "verified",
        ):
            cp = _make_checkpoint(comment_phase=phase)
            assert cp.comment_phase == phase

    def test_comment_phase_invalid_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_checkpoint(comment_phase="invalid_phase")

    def test_comment_phase_in_markdown(self):
        cp = _make_checkpoint(
            current_phase="comment_on_post",
            current_step_index=4,
            comment_phase="typed",
        )
        md = checkpoint_to_markdown(cp)
        assert "comment: typed" in md
        assert "comment_on_post" in md

    def test_comment_phase_absent_in_markdown_when_none(self):
        cp = _make_checkpoint(
            current_phase="comment_on_post",
            current_step_index=2,
        )
        md = checkpoint_to_markdown(cp)
        assert "comment:" not in md
