"""Tests for leashd.skills — skill management."""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.skills import (
    SkillInfo,
    _parse_frontmatter,
    _safe_extractall,
    get_skill,
    get_skills_by_tag,
    has_installed_skills,
    install_skill,
    list_skills,
    remove_skill,
    validate_skill_zip,
)


@pytest.fixture
def fake_config_dir(tmp_path):
    """Redirect config_path() to a temp directory."""
    fake_path = tmp_path / ".leashd" / "config.yaml"
    with patch("leashd.config_store._CONFIG_FILE", fake_path):
        yield fake_path


@pytest.fixture
def fake_skills_dir(tmp_path):
    """Redirect skills installation directory to temp."""
    skills_dir = tmp_path / "skills"
    with patch("leashd.skills._SKILLS_DIR", skills_dir):
        yield skills_dir


def _make_skill_zip(
    tmp_path: Path,
    name: str = "test-skill",
    description: str = "A test skill",
    *,
    nested: bool = False,
    extra_files: dict[str, str] | None = None,
    missing_skill_md: bool = False,
    bad_frontmatter: bool = False,
    no_name: bool = False,
    no_description: bool = False,
) -> Path:
    """Helper to create a skill zip file."""
    zip_path = tmp_path / f"{name}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        if not missing_skill_md:
            fm_parts = []
            if not bad_frontmatter:
                fm_parts.append("---")
                if not no_name:
                    fm_parts.append(f"name: {name}")
                if not no_description:
                    fm_parts.append(f"description: {description}")
                fm_parts.append("---")
                fm_parts.append("")
                fm_parts.append("# Instructions")
                fm_parts.append("Do something useful.")
            else:
                fm_parts.append("not yaml at all {{{{")

            content = "\n".join(fm_parts)
            prefix = f"{name}/" if nested else ""
            zf.writestr(f"{prefix}SKILL.md", content)

        if extra_files:
            for fname, fcontent in extra_files.items():
                prefix = f"{name}/" if nested else ""
                zf.writestr(f"{prefix}{fname}", fcontent)

    return zip_path


class TestParseFrontmatter:
    def test_valid(self):
        text = "---\nname: foo\ndescription: bar\n---\n# Body"
        result = _parse_frontmatter(text)
        assert result["name"] == "foo"
        assert result["description"] == "bar"

    def test_no_opening_marker(self):
        assert _parse_frontmatter("name: foo") == {}

    def test_no_closing_marker(self):
        assert _parse_frontmatter("---\nname: foo\n") == {}

    def test_invalid_yaml(self):
        assert _parse_frontmatter("---\nname: foo\nname: [1, 2\n---\n") == {}

    def test_non_string_scalar_frontmatter(self):
        assert _parse_frontmatter("---\n:::bad{{\n---\n") == {}

    def test_non_dict_frontmatter(self):
        assert _parse_frontmatter("---\n- a\n- b\n---\n") == {}


class TestValidateSkillZip:
    def test_valid_root_skill_md(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path)
        name, desc, rel_dir = validate_skill_zip(zip_path)
        assert name == "test-skill"
        assert desc == "A test skill"
        assert rel_dir == ""

    def test_valid_nested_skill_md(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path, nested=True)
        name, _desc, rel_dir = validate_skill_zip(zip_path)
        assert name == "test-skill"
        assert rel_dir == "test-skill"

    def test_missing_skill_md(self, tmp_path):
        zip_path = _make_skill_zip(
            tmp_path, missing_skill_md=True, extra_files={"readme.md": "hi"}
        )
        with pytest.raises(ValueError, match=r"No SKILL\.md found"):
            validate_skill_zip(zip_path)

    def test_invalid_frontmatter(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path, bad_frontmatter=True)
        with pytest.raises(ValueError, match="missing required 'name'"):
            validate_skill_zip(zip_path)

    def test_missing_name(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path, no_name=True)
        with pytest.raises(ValueError, match="missing required 'name'"):
            validate_skill_zip(zip_path)

    def test_missing_description(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path, no_description=True)
        with pytest.raises(ValueError, match="missing required 'description'"):
            validate_skill_zip(zip_path)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            validate_skill_zip(tmp_path / "nope.zip")

    def test_invalid_name_uppercase(self, tmp_path):
        zip_path = _make_skill_zip(tmp_path, name="BadName")
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_zip(zip_path)

    def test_name_too_long(self, tmp_path):
        long_name = "a" * 65
        zip_path = _make_skill_zip(tmp_path, name=long_name)
        with pytest.raises(ValueError, match="too long"):
            validate_skill_zip(zip_path)


class TestInstallSkill:
    def test_successful_install(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip_path = _make_skill_zip(tmp_path, extra_files={"helper.py": "# helper"})
        skill = install_skill(zip_path, tags=["web", "content"])
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"
        assert skill.tags == ["web", "content"]
        assert (fake_skills_dir / "test-skill" / "SKILL.md").is_file()
        assert (fake_skills_dir / "test-skill" / "helper.py").is_file()

    def test_overwrite_existing(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip1 = _make_skill_zip(tmp_path / "v1", name="my-skill", description="v1")
        install_skill(zip1)
        assert get_skill("my-skill").description == "v1"

        zip2_dir = tmp_path / "v2"
        zip2_dir.mkdir()
        zip2 = _make_skill_zip(zip2_dir, name="my-skill", description="v2")
        install_skill(zip2)
        assert get_skill("my-skill").description == "v2"

    def test_invalid_zip_raises(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip_path = _make_skill_zip(tmp_path, missing_skill_md=True)
        with pytest.raises(ValueError, match=r"No SKILL\.md found"):
            install_skill(zip_path)

    def test_install_nested(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip_path = _make_skill_zip(tmp_path, nested=True)
        skill = install_skill(zip_path)
        assert (fake_skills_dir / "test-skill" / "SKILL.md").is_file()
        assert skill.name == "test-skill"

    def test_no_tags(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip_path = _make_skill_zip(tmp_path)
        skill = install_skill(zip_path)
        assert skill.tags == []


class TestRemoveSkill:
    def test_remove_existing(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip_path = _make_skill_zip(tmp_path)
        install_skill(zip_path)
        assert remove_skill("test-skill") is True
        assert not (fake_skills_dir / "test-skill").exists()
        assert get_skill("test-skill") is None

    def test_remove_nonexistent(self, fake_config_dir, fake_skills_dir):
        assert remove_skill("nope") is False


class TestListSkills:
    def test_empty(self, fake_config_dir):
        assert list_skills() == []

    def test_lists_installed(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip1 = _make_skill_zip(tmp_path / "a", name="skill-a", description="Skill A")
        zip2_dir = tmp_path / "b"
        zip2_dir.mkdir()
        zip2 = _make_skill_zip(zip2_dir, name="skill-b", description="Skill B")
        install_skill(zip1)
        install_skill(zip2, tags=["web"])

        skills = list_skills()
        names = {s.name for s in skills}
        assert names == {"skill-a", "skill-b"}


class TestGetSkillsByTag:
    def test_filter_by_tag(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip1 = _make_skill_zip(tmp_path / "a", name="skill-a", description="A")
        zip2_dir = tmp_path / "b"
        zip2_dir.mkdir()
        zip2 = _make_skill_zip(zip2_dir, name="skill-b", description="B")
        install_skill(zip1, tags=["web"])
        install_skill(zip2, tags=["other"])

        web_skills = get_skills_by_tag("web")
        assert len(web_skills) == 1
        assert web_skills[0].name == "skill-a"

    def test_no_matches(self, fake_config_dir):
        assert get_skills_by_tag("nonexistent") == []


class TestHasInstalledSkills:
    def test_false_when_empty(self, fake_config_dir):
        assert has_installed_skills() is False

    def test_true_when_installed(self, tmp_path, fake_config_dir, fake_skills_dir):
        zip_path = _make_skill_zip(tmp_path)
        install_skill(zip_path)
        assert has_installed_skills() is True


class TestSkillInfo:
    def test_frozen(self):
        from pydantic import ValidationError

        info = SkillInfo(
            name="test",
            description="desc",
            installed_at="2026-01-01",
            source="/tmp/test.zip",
        )
        with pytest.raises(ValidationError, match="frozen"):
            info.name = "other"  # type: ignore[misc]

    def test_default_tags(self):
        info = SkillInfo(
            name="test",
            description="desc",
            installed_at="2026-01-01",
            source="/tmp/test.zip",
        )
        assert info.tags == []


class TestZipSlip:
    def test_zip_slip_blocked(self, tmp_path):
        """Malicious zip with path traversal must be rejected."""
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../evil.txt", "pwned")
        target = tmp_path / "extract"
        target.mkdir()
        with (
            zipfile.ZipFile(zip_path) as zf,
            pytest.raises(ValueError, match="Zip path traversal blocked"),
        ):
            _safe_extractall(zf, target)
        assert not (tmp_path / "evil.txt").exists()

    def test_safe_zip_extracts(self, tmp_path):
        """Normal zip extracts without issue."""
        zip_path = tmp_path / "safe.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("subdir/file.txt", "ok")
        target = tmp_path / "extract"
        target.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, target)
        assert (target / "subdir" / "file.txt").read_text() == "ok"


class TestPathTraversal:
    def test_remove_traversal_blocked(self, fake_config_dir, fake_skills_dir):
        with pytest.raises(ValueError, match="Invalid skill name"):
            remove_skill("../../etc")

    def test_get_skill_traversal_blocked(self, fake_config_dir):
        with pytest.raises(ValueError, match="Invalid skill name"):
            get_skill("../../etc")


@pytest.fixture
def no_agent_browser_cli(monkeypatch):
    """Force the vendored-copy fallback regardless of the host machine."""
    monkeypatch.setattr("leashd.skills._agent_browser_cli_output", lambda *args: None)


@pytest.fixture
def fake_agent_browser_cli(monkeypatch, tmp_path):
    """Stand in for an installed agent-browser shipping its own core skill."""
    source = tmp_path / "skill-data" / "core"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: agent-browser\n---\n# from CLI\n")

    state = {"version": "0.33.0"}

    def fake_output(*args: str) -> str | None:
        if args[:2] == ("skills", "path"):
            return str(source)
        if args == ("--version",):
            return f"agent-browser {state['version']}"
        return None

    monkeypatch.setattr("leashd.skills._agent_browser_cli_output", fake_output)
    return state


class TestBuiltinAgentBrowserSkill:
    def test_ensure_installs_skill(
        self, fake_config_dir, fake_skills_dir, no_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        skill_md = fake_skills_dir / "agent-browser" / "SKILL.md"
        assert skill_md.is_file()

    def test_ensure_idempotent(
        self, fake_config_dir, fake_skills_dir, no_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        ensure_agent_browser_skill()
        assert (fake_skills_dir / "agent-browser" / "SKILL.md").is_file()

    def test_ensure_saves_metadata(
        self, fake_config_dir, fake_skills_dir, no_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        info = get_skill("agent-browser")
        assert info is not None
        assert info.source == "agent-browser@builtin"

    def test_prefers_installed_cli_skill_over_vendored_copy(
        self, fake_config_dir, fake_skills_dir, fake_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()

        skill_md = fake_skills_dir / "agent-browser" / "SKILL.md"
        assert "# from CLI" in skill_md.read_text()
        info = get_skill("agent-browser")
        assert info is not None
        assert info.source == "agent-browser@0.33.0"

    def test_refreshes_when_cli_version_changes(
        self, fake_config_dir, fake_skills_dir, fake_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        assert get_skill("agent-browser").source == "agent-browser@0.33.0"

        fake_agent_browser_cli["version"] = "0.34.0"
        ensure_agent_browser_skill()

        assert get_skill("agent-browser").source == "agent-browser@0.34.0"

    def test_no_recopy_when_version_unchanged(
        self, fake_config_dir, fake_skills_dir, fake_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        marker = fake_skills_dir / "agent-browser" / "local-edit.txt"
        marker.write_text("kept")

        ensure_agent_browser_skill()

        assert marker.is_file()

    def test_falls_back_when_cli_path_is_unusable(
        self, fake_config_dir, fake_skills_dir, monkeypatch
    ):
        from leashd.skills import ensure_agent_browser_skill

        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: "/nonexistent/skill/dir",
        )

        ensure_agent_browser_skill()

        assert (fake_skills_dir / "agent-browser" / "SKILL.md").is_file()
        assert get_skill("agent-browser").source == "agent-browser@builtin"

    def test_remove_deletes_skill(
        self, fake_config_dir, fake_skills_dir, no_agent_browser_cli
    ):
        from leashd.skills import ensure_agent_browser_skill, remove_agent_browser_skill

        ensure_agent_browser_skill()
        remove_agent_browser_skill()
        assert not (fake_skills_dir / "agent-browser").exists()
        assert get_skill("agent-browser") is None

    def test_remove_noop_when_not_installed(self, fake_config_dir, fake_skills_dir):
        from leashd.skills import remove_agent_browser_skill

        remove_agent_browser_skill()  # should not raise

    def test_ensure_replaces_partial_install(
        self, fake_config_dir, fake_skills_dir, no_agent_browser_cli
    ):
        """A target dir without SKILL.md (interrupted install) is wiped and
        re-copied rather than left half-populated."""
        from leashd.skills import ensure_agent_browser_skill

        stale = fake_skills_dir / "agent-browser"
        stale.mkdir(parents=True)
        (stale / "leftover.txt").write_text("stale")

        ensure_agent_browser_skill()

        assert (stale / "SKILL.md").is_file()
        assert not (stale / "leftover.txt").exists()


class TestAgentBrowserSkillNameNormalization:
    def test_cli_skill_installs_under_agent_browser_name(
        self, fake_config_dir, fake_skills_dir, fake_agent_browser_cli
    ):
        # The CLI names its copy `core`; installed under
        # ~/.claude/skills/agent-browser that would leave the frontmatter
        # disagreeing with the directory and the metadata key.
        from leashd.skills import _parse_frontmatter, ensure_agent_browser_skill

        ensure_agent_browser_skill()

        skill_md = fake_skills_dir / "agent-browser" / "SKILL.md"
        assert _parse_frontmatter(skill_md.read_text())["name"] == "agent-browser"

    def test_description_and_body_preserved(
        self, fake_config_dir, fake_skills_dir, monkeypatch, tmp_path
    ):
        from leashd.skills import _parse_frontmatter, ensure_agent_browser_skill

        source = tmp_path / "core"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: core\ndescription: Core guide\n"
            "allowed-tools: Bash(agent-browser:*)\n---\n\n# Body kept\n"
        )
        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: (
                str(source)
                if args[:2] == ("skills", "path")
                else "agent-browser 0.33.0"
            ),
        )

        ensure_agent_browser_skill()

        text = (fake_skills_dir / "agent-browser" / "SKILL.md").read_text()
        fm = _parse_frontmatter(text)
        assert fm["name"] == "agent-browser"
        assert fm["description"] == "Core guide"
        assert fm["allowed-tools"] == "Bash(agent-browser:*)"
        assert "# Body kept" in text


class TestAgentBrowserArtifactPaths:
    """Upstream examples write to /tmp; leashd keeps artifacts in .leashd.

    Regression: syncing the vendored skill from the CLI overwrote leashd's
    `.leashd` customisation, so the shipped templates told agents to save
    screenshots into a directory temp cleanup reclaims. Applied on install so
    it survives the next CLI upgrade instead of being a hand-patch.
    """

    def test_rewrites_tmp_paths(self, tmp_path):
        from leashd.skills import _normalize_artifact_paths

        (tmp_path / "SKILL.md").write_text(
            "agent-browser screenshot /tmp/page.png\n"
            "agent-browser network har stop /tmp/trace.har\n"
        )
        assert _normalize_artifact_paths(tmp_path) == 1
        text = (tmp_path / "SKILL.md").read_text()
        assert "/tmp/" not in text
        assert "screenshot .leashd/page.png" in text
        assert "har stop .leashd/trace.har" in text

    def test_rewrites_shell_output_dir_default(self, tmp_path):
        from leashd.skills import _normalize_artifact_paths

        script = tmp_path / "capture.sh"
        script.write_text('OUTPUT_DIR="${2:-.}"\n')
        _normalize_artifact_paths(tmp_path)
        assert script.read_text() == 'OUTPUT_DIR="${2:-.leashd}"\n'

    def test_recurses_into_subdirectories(self, tmp_path):
        from leashd.skills import _normalize_artifact_paths

        nested = tmp_path / "references"
        nested.mkdir()
        (nested / "session.md").write_text("agent-browser screenshot /tmp/a.png\n")
        assert _normalize_artifact_paths(tmp_path) == 1
        assert "/tmp/" not in (nested / "session.md").read_text()

    def test_leaves_unrelated_files_alone(self, tmp_path):
        from leashd.skills import _normalize_artifact_paths

        binary = tmp_path / "logo.png"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n/tmp/keepme")
        (tmp_path / "notes.txt").write_text("/tmp/keepme\n")
        assert _normalize_artifact_paths(tmp_path) == 0
        assert b"/tmp/keepme" in binary.read_bytes()
        assert "/tmp/keepme" in (tmp_path / "notes.txt").read_text()

    def test_idempotent(self, tmp_path):
        from leashd.skills import _normalize_artifact_paths

        (tmp_path / "SKILL.md").write_text("screenshot /tmp/a.png\n")
        assert _normalize_artifact_paths(tmp_path) == 1
        assert _normalize_artifact_paths(tmp_path) == 0

    def test_applied_on_install_from_cli_source(
        self, fake_config_dir, fake_skills_dir, monkeypatch, tmp_path
    ):
        from leashd.skills import ensure_agent_browser_skill

        source = tmp_path / "core"
        (source / "templates").mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: core\ndescription: d\n---\n\nscreenshot /tmp/x.png\n"
        )
        (source / "templates" / "form.sh").write_text(
            "agent-browser screenshot /tmp/form-result.png\n"
        )
        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: (
                str(source)
                if args[:2] == ("skills", "path")
                else "agent-browser 0.33.0"
            ),
        )

        ensure_agent_browser_skill()

        installed = fake_skills_dir / "agent-browser"
        assert "/tmp/" not in (installed / "SKILL.md").read_text()
        assert "/tmp/" not in (installed / "templates" / "form.sh").read_text()

    def test_vendored_skill_ships_no_tmp_paths(self):
        from leashd.skills import _BUILTIN_SKILL_DATA

        offenders = [
            p
            for p in (_BUILTIN_SKILL_DATA / "agent-browser").rglob("*")
            if p.is_file()
            and p.suffix in {".md", ".sh"}
            and "/tmp/" in p.read_text(encoding="utf-8", errors="ignore")
        ]
        assert offenders == []


def _apply_tab_discipline(root):
    from leashd.skills import (
        _TAB_DISCIPLINE_MARKER,
        _TAB_DISCIPLINE_SECTION,
        _upsert_section,
    )

    return _upsert_section(root, _TAB_DISCIPLINE_MARKER, _TAB_DISCIPLINE_SECTION)


def _apply_search_engine(root):
    from leashd.skills import (
        _SEARCH_ENGINE_MARKER,
        _SEARCH_ENGINE_SECTION,
        _upsert_section,
    )

    return _upsert_section(root, _SEARCH_ENGINE_MARKER, _SEARCH_ENGINE_SECTION)


def _apply_batched_search(root):
    from leashd.skills import (
        _BATCHED_SEARCH_MARKER,
        _BATCHED_SEARCH_SECTION,
        _upsert_section,
    )

    return _upsert_section(root, _BATCHED_SEARCH_MARKER, _BATCHED_SEARCH_SECTION)


class TestAgentBrowserTabDiscipline:
    """Upstream documents `--session` fan-out but caps nothing.

    Regression: a `/web` run looped `agent-browser open`/`close` over a URL
    list, and under the headed shared profile each relaunch left a window
    behind until a launch restored ~200 of them. The cap is reapplied on every
    install so a CLI upgrade cannot drop it.
    """

    def test_appends_section(self, tmp_path):
        from leashd.skills import MAX_BROWSER_TABS

        (tmp_path / "SKILL.md").write_text("---\nname: core\n---\n\nbody\n")
        assert _apply_tab_discipline(tmp_path) is True
        text = (tmp_path / "SKILL.md").read_text()
        assert "## One browser, many tabs" in text
        assert f"at most {MAX_BROWSER_TABS} tabs" in text
        assert "agent-browser tab new" in text
        assert text.startswith("---\nname: core\n---\n\nbody")

    def test_warns_against_session_fanout(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body\n")
        _apply_tab_discipline(tmp_path)
        text = " ".join((tmp_path / "SKILL.md").read_text().split())
        assert "never use sessions to fan out over a URL list" in text
        assert "Never call `agent-browser close` between pages" in text

    def test_idempotent(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body\n")
        assert _apply_tab_discipline(tmp_path) is True
        assert _apply_tab_discipline(tmp_path) is False
        text = (tmp_path / "SKILL.md").read_text()
        assert text.count("## One browser, many tabs") == 1

    def test_missing_skill_md(self, tmp_path):
        assert _apply_tab_discipline(tmp_path) is False

    def test_applied_on_install_from_cli_source(
        self, fake_config_dir, fake_skills_dir, monkeypatch, tmp_path
    ):
        from leashd.skills import ensure_agent_browser_skill

        source = tmp_path / "core"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: core\ndescription: d\n---\n\nagent-browser open url\n"
        )
        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: (
                str(source)
                if args[:2] == ("skills", "path")
                else "agent-browser 0.33.0"
            ),
        )

        ensure_agent_browser_skill()

        from leashd.skills import MAX_BROWSER_TABS

        text = (fake_skills_dir / "agent-browser" / "SKILL.md").read_text()
        assert "## One browser, many tabs" in text
        assert f"at most {MAX_BROWSER_TABS} tabs" in text

    def test_backfilled_into_existing_install(
        self, fake_config_dir, fake_skills_dir, monkeypatch, tmp_path
    ):
        """An install predating the cap is not reinstalled — the CLI version
        still matches — so the rule has to be backfilled in place."""
        from leashd.skills import ensure_agent_browser_skill, save_skill_metadata

        source = tmp_path / "core"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("---\nname: core\ndescription: d\n---\n")
        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: (
                str(source)
                if args[:2] == ("skills", "path")
                else "agent-browser 0.33.0"
            ),
        )
        stale = fake_skills_dir / "agent-browser"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("---\nname: agent-browser\n---\n\nold body\n")
        save_skill_metadata(
            name="agent-browser",
            description="Browser automation CLI for AI agents",
            source="agent-browser@0.33.0",
            installed_at="2026-07-25T06:48:23+00:00",
            tags=["browser"],
        )

        ensure_agent_browser_skill()

        text = (stale / "SKILL.md").read_text()
        assert "old body" in text
        assert "## One browser, many tabs" in text


class TestAgentBrowserSearchEngineDefault:
    """Upstream's worked search example opens DuckDuckGo.

    Agents copied it, then lost `site:` and `&num=` partway through a research
    run and switched to Google mid-task. Reapplied on every install for the
    same reason as the tab cap.
    """

    def test_appends_section(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("---\nname: core\n---\n\nbody\n")
        assert _apply_search_engine(tmp_path) is True
        text = (tmp_path / "SKILL.md").read_text()
        assert "## Default search engine" in text
        assert "https://www.google.com/search?q=<query>&num=20" in text
        assert text.startswith("---\nname: core\n---\n\nbody")

    def test_keeps_fallbacks_secondary(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body\n")
        _apply_search_engine(tmp_path)
        text = (tmp_path / "SKILL.md").read_text()
        assert "Fall back to" in text
        assert text.index("google.com/search") < text.index("duckduckgo.com/?q=")

    def test_idempotent(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body\n")
        assert _apply_search_engine(tmp_path) is True
        assert _apply_search_engine(tmp_path) is False
        assert (tmp_path / "SKILL.md").read_text().count(
            "## Default search engine"
        ) == 1

    def test_missing_skill_md(self, tmp_path):
        assert _apply_search_engine(tmp_path) is False

    def test_coexists_with_tab_discipline(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body\n")
        _apply_tab_discipline(tmp_path)
        _apply_search_engine(tmp_path)
        text = (tmp_path / "SKILL.md").read_text()
        assert "## One browser, many tabs" in text
        assert "## Default search engine" in text

    def test_backfilled_into_existing_install(
        self, fake_config_dir, fake_skills_dir, monkeypatch, tmp_path
    ):
        from leashd.skills import ensure_agent_browser_skill, save_skill_metadata

        source = tmp_path / "core"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("---\nname: core\ndescription: d\n---\n")
        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: (
                str(source)
                if args[:2] == ("skills", "path")
                else "agent-browser 0.33.0"
            ),
        )
        stale = fake_skills_dir / "agent-browser"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("---\nname: agent-browser\n---\n\nold body\n")
        save_skill_metadata(
            name="agent-browser",
            description="Browser automation CLI for AI agents",
            source="agent-browser@0.33.0",
            installed_at="2026-07-25T06:48:23+00:00",
            tags=["browser"],
        )

        ensure_agent_browser_skill()

        text = (stale / "SKILL.md").read_text()
        assert "old body" in text
        assert "## Default search engine" in text


class TestUpsertSection:
    """Sections are refreshed in place, not appended once and frozen.

    Regression: the appended sections were only written when their marker was
    absent, so every already-installed skill stayed pinned to whatever wording
    shipped first and later edits reached nobody.
    """

    def test_replaces_stale_wording(self, tmp_path):
        from leashd.skills import _upsert_section

        (tmp_path / "SKILL.md").write_text(
            "body\n\n## Topic\n\nold wording\n\n## Later\n\nkeep me\n"
        )

        assert _upsert_section(tmp_path, "## Topic", "\n## Topic\n\nnew wording\n")

        text = (tmp_path / "SKILL.md").read_text()
        assert "new wording" in text
        assert "old wording" not in text
        assert text.count("## Topic") == 1

    def test_preserves_following_sections(self, tmp_path):
        from leashd.skills import _upsert_section

        (tmp_path / "SKILL.md").write_text("## Topic\n\nold\n\n## Later\n\nkeep me\n")

        _upsert_section(tmp_path, "## Topic", "\n## Topic\n\nnew\n")

        text = (tmp_path / "SKILL.md").read_text()
        assert "## Later" in text
        assert "keep me" in text
        assert text.index("## Topic") < text.index("## Later")

    def test_unchanged_content_reports_no_edit(self, tmp_path):
        from leashd.skills import _upsert_section

        (tmp_path / "SKILL.md").write_text("body\n")
        section = "\n## Topic\n\nwording\n"

        assert _upsert_section(tmp_path, "## Topic", section) is True
        assert _upsert_section(tmp_path, "## Topic", section) is False

    def test_missing_skill_md(self, tmp_path):
        from leashd.skills import _upsert_section

        assert _upsert_section(tmp_path, "## Topic", "\n## Topic\n\nx\n") is False


class TestAgentBrowserBatchedSearch:
    """Google refuses result pages that arrive in a burst.

    Measured: 15 tabs opened back-to-back lost 2 pages to `/sorry/index`, the
    same 15 queries run one tab at a time lost none. Agents hand-rolled `for`
    loops that open every tab at once, so the throttled helper ships with the
    skill and the guidance points at it.
    """

    def test_section_documents_the_helper(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("body\n")

        assert _apply_batched_search(tmp_path) is True

        text = (tmp_path / "SKILL.md").read_text()
        assert "## Batched searches" in text
        assert "templates/search-batch.sh" in text
        assert "-j 2:5" in text

    def test_script_installed_executable(self, tmp_path):
        from leashd.skills import _install_search_script

        assert _install_search_script(tmp_path) is True

        script = tmp_path / "templates" / "search-batch.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111
        assert script.read_text().startswith("#!/bin/bash")

    def test_script_install_idempotent(self, tmp_path):
        from leashd.skills import _install_search_script

        assert _install_search_script(tmp_path) is True
        assert _install_search_script(tmp_path) is False

    def test_script_refreshed_when_stale(self, tmp_path):
        from leashd.skills import _install_search_script

        stale = tmp_path / "templates" / "search-batch.sh"
        stale.parent.mkdir(parents=True)
        stale.write_text("#!/bin/bash\nold\n")

        assert _install_search_script(tmp_path) is True
        assert "old" not in stale.read_text()

    def test_script_never_closes_the_browser(self):
        from leashd.skills import _SEARCH_SCRIPT_SOURCE

        text = _SEARCH_SCRIPT_SOURCE.read_text()
        assert "agent-browser close" not in text
        assert "tab close" in text

    def test_applied_on_install(
        self, fake_config_dir, fake_skills_dir, monkeypatch, tmp_path
    ):
        from leashd.skills import ensure_agent_browser_skill

        source = tmp_path / "core"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: core\ndescription: d\n---\n\nbody\n"
        )
        monkeypatch.setattr(
            "leashd.skills._agent_browser_cli_output",
            lambda *args: (
                str(source)
                if args[:2] == ("skills", "path")
                else "agent-browser 0.33.0"
            ),
        )

        ensure_agent_browser_skill()

        installed = fake_skills_dir / "agent-browser"
        assert "## Batched searches" in (installed / "SKILL.md").read_text()
        assert (installed / "templates" / "search-batch.sh").is_file()

    def test_lives_inside_the_installed_package(self):
        """`_install_search_script` fails silently when the source is absent, so
        a helper kept outside the package would only show up as agents
        hand-rolling `for` loops again on an installed leashd."""
        import leashd
        from leashd.skills import _SEARCH_SCRIPT_SOURCE

        assert _SEARCH_SCRIPT_SOURCE.is_file()
        assert _SEARCH_SCRIPT_SOURCE.is_relative_to(Path(leashd.__file__).parent)


_HAPPY_PATH_BROWSER = """#!/bin/bash
echo "$*" >> "$STUB_LOG"
if [ "$1 $2" = "get url" ]; then
    echo "https://www.google.com/search?q=x&num=20"
    exit 0
fi
if [ "$1" = "eval" ]; then
    echo '{"data":{"result":"[{\\"t\\":\\"Title One\\",\\"u\\":\\"https://example.com/1\\"}]"}}'
    exit 0
fi
exit 0
"""


_BLOCKING_BROWSER = """#!/bin/bash
echo "$*" >> "$STUB_LOG"
for arg in "$@"; do
    case "$arg" in
        *google.com*) echo google > "$STUB_STATE" ;;
        *duckduckgo*) echo duckduckgo > "$STUB_STATE" ;;
    esac
done
engine="$(cat "$STUB_STATE" 2>/dev/null || echo google)"
if [ "$1 $2" = "get url" ]; then
    if [ "$engine" = google ]; then
        echo "https://www.google.com/sorry/index?continue=x"
    else
        echo "https://duckduckgo.com/?q=x"
    fi
    exit 0
fi
if [ "$1" = "eval" ]; then
    if [ "$engine" = google ]; then
        echo '{"data":{"result":"[]"}}'
    else
        echo '{"data":{"result":"[{\\"t\\":\\"DDG\\",\\"u\\":\\"https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Freal\\"}]"}}'
    fi
    exit 0
fi
exit 0
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
class TestSearchBatchScriptBehaviour:
    """Drive the shipped helper against a stub `agent-browser`.

    The section text above only proves the guidance mentions the script. This
    exercises the thing the agent actually runs: the in-flight cap that keeps
    Google from serving `/sorry`, the per-query tab close, and the engine
    fallback — none of which a wording assertion can catch.
    """

    def _run(self, tmp_path, stub_body, *args):
        from leashd.skills import _SEARCH_SCRIPT_SOURCE

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "agent-browser"
        stub.write_text(stub_body)
        stub.chmod(0o755)
        log = tmp_path / "calls.log"
        log.touch()

        result = subprocess.run(
            ["bash", str(_SEARCH_SCRIPT_SOURCE), "-j", "0:0", *args],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "STUB_LOG": str(log),
                "STUB_STATE": str(tmp_path / "state"),
            },
        )
        return result, log.read_text().splitlines()

    def test_is_valid_bash(self):
        from leashd.skills import _SEARCH_SCRIPT_SOURCE

        assert (
            subprocess.run(
                ["bash", "-n", str(_SEARCH_SCRIPT_SOURCE)], capture_output=True
            ).returncode
            == 0
        )

    def test_prints_ranked_results_per_query(self, tmp_path):
        result, _ = self._run(tmp_path, _HAPPY_PATH_BROWSER, "-n", "2", "alpha", "beta")

        assert result.returncode == 0, result.stderr
        assert "## alpha\t[google]" in result.stdout
        assert "## beta\t[google]" in result.stdout
        assert result.stdout.count("Title One\thttps://example.com/1") == 2

    def test_searches_google_with_the_wider_result_page(self, tmp_path):
        _, calls = self._run(tmp_path, _HAPPY_PATH_BROWSER, "alpha query")

        opens = [c for c in calls if c.startswith("tab new")]
        assert opens == [
            "tab new --label sb0 https://www.google.com/search?q=alpha+query&num=20"
        ]

    def test_caps_tabs_in_flight_and_closes_each_as_it_is_read(self, tmp_path):
        """The whole point of the helper: a burst of opens is what Google
        refuses, so only `-n` tabs may exist before the first close."""
        _, calls = self._run(
            tmp_path, _HAPPY_PATH_BROWSER, "-n", "2", "a", "b", "c", "d", "e"
        )

        first_close = next(i for i, c in enumerate(calls) if c.startswith("tab close"))
        assert len([c for c in calls[:first_close] if c.startswith("tab new")]) == 2
        assert len([c for c in calls if c.startswith("tab close")]) == 5

    def test_never_closes_the_browser_itself(self, tmp_path):
        """`agent-browser close` is what leaks a window into the profile's
        restore state, and the next launch replays every one of them."""
        _, calls = self._run(tmp_path, _HAPPY_PATH_BROWSER, "-n", "2", "a", "b", "c")

        assert [c for c in calls if c.split()[0] == "close"] == []

    def test_falls_back_to_duckduckgo_when_google_serves_an_interstitial(
        self, tmp_path
    ):
        result, calls = self._run(tmp_path, _BLOCKING_BROWSER, "-n", "1", "alpha")

        assert "## alpha\t[duckduckgo]" in result.stdout
        assert "https://duckduckgo.com/?q=alpha" in " ".join(calls)
        assert "served an interstitial" in result.stderr

    def test_unwraps_the_fallback_engine_redirect(self, tmp_path):
        """DuckDuckGo hands back `/l/?uddg=…` wrappers; an agent that reports
        those as sources has cited the search engine, not the page."""
        result, _ = self._run(tmp_path, _BLOCKING_BROWSER, "-n", "1", "alpha")

        assert "https://example.org/real" in result.stdout
        assert "uddg=" not in result.stdout

    def test_reads_queries_from_a_file(self, tmp_path):
        queries = tmp_path / "queries.txt"
        queries.write_text("first query\n\nsecond query\n")

        result, calls = self._run(tmp_path, _HAPPY_PATH_BROWSER, "-f", str(queries))

        assert result.stdout.count("## ") == 2
        assert len([c for c in calls if c.startswith("tab new")]) == 2

    def test_rejects_an_unknown_engine_before_opening_anything(self, tmp_path):
        result, calls = self._run(
            tmp_path, _HAPPY_PATH_BROWSER, "-e", "askjeeves", "alpha"
        )

        assert result.returncode == 1
        assert "unknown engine" in result.stderr
        assert calls == []

    def test_usage_without_queries(self, tmp_path):
        result, calls = self._run(tmp_path, _HAPPY_PATH_BROWSER)

        assert result.returncode == 1
        assert "Usage:" in result.stdout
        assert calls == []
