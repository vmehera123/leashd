"""Tests for leashd.skills — skill management."""

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


class TestBuiltinAgentBrowserSkill:
    def test_ensure_installs_skill(self, fake_config_dir, fake_skills_dir):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        skill_md = fake_skills_dir / "agent-browser" / "SKILL.md"
        assert skill_md.is_file()

    def test_ensure_idempotent(self, fake_config_dir, fake_skills_dir):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        ensure_agent_browser_skill()
        assert (fake_skills_dir / "agent-browser" / "SKILL.md").is_file()

    def test_ensure_saves_metadata(self, fake_config_dir, fake_skills_dir):
        from leashd.skills import ensure_agent_browser_skill

        ensure_agent_browser_skill()
        info = get_skill("agent-browser")
        assert info is not None
        assert info.source == "builtin"

    def test_remove_deletes_skill(self, fake_config_dir, fake_skills_dir):
        from leashd.skills import ensure_agent_browser_skill, remove_agent_browser_skill

        ensure_agent_browser_skill()
        remove_agent_browser_skill()
        assert not (fake_skills_dir / "agent-browser").exists()
        assert get_skill("agent-browser") is None

    def test_remove_noop_when_not_installed(self, fake_config_dir, fake_skills_dir):
        from leashd.skills import remove_agent_browser_skill

        remove_agent_browser_skill()  # should not raise
