"""Tests for leashd.cc_plugins — Claude Code plugin management."""

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.cc_plugins import (
    PluginInfo,
    _safe_extractall,
    disable_plugin,
    enable_plugin,
    get_enabled_plugin_paths,
    get_plugin,
    has_installed_plugins,
    install_plugin,
    list_plugins,
    remove_plugin,
    validate_plugin_dir,
    validate_plugin_zip,
)


@pytest.fixture
def fake_config_dir(tmp_path):
    """Redirect config_path() to a temp directory."""
    fake_path = tmp_path / ".leashd" / "config.yaml"
    with patch("leashd.config_store._CONFIG_FILE", fake_path):
        yield fake_path


@pytest.fixture
def fake_plugins_dir(tmp_path):
    """Redirect plugins installation directory to temp."""
    plugins_dir = tmp_path / "plugins"
    with patch("leashd.cc_plugins._PLUGINS_DIR", plugins_dir):
        yield plugins_dir


def _make_plugin_dir(
    tmp_path: Path,
    name: str = "test-plugin",
    description: str = "A test plugin",
    version: str = "1.0.0",
    author: str = "Test Author",
    *,
    extra_files: dict[str, str] | None = None,
    missing_manifest: bool = False,
    invalid_json: bool = False,
    missing_name: bool = False,
    missing_description: bool = False,
    missing_version: bool = False,
    missing_author: bool = False,
) -> Path:
    """Helper to create a plugin directory with .claude-plugin/plugin.json."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    if not missing_manifest:
        manifest_dir = plugin_dir / ".claude-plugin"
        manifest_dir.mkdir(parents=True, exist_ok=True)

        if invalid_json:
            (manifest_dir / "plugin.json").write_text("not json at all {{{")
        else:
            manifest: dict = {}
            if not missing_name:
                manifest["name"] = name
            if not missing_description:
                manifest["description"] = description
            if not missing_version:
                manifest["version"] = version
            if not missing_author:
                manifest["author"] = author
            (manifest_dir / "plugin.json").write_text(json.dumps(manifest))

    if extra_files:
        for fname, fcontent in extra_files.items():
            fpath = plugin_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(fcontent)

    return plugin_dir


def _make_plugin_zip(
    tmp_path: Path,
    name: str = "test-plugin",
    description: str = "A test plugin",
    version: str = "1.0.0",
    author: str = "Test Author",
    *,
    nested: bool = False,
    extra_files: dict[str, str] | None = None,
    missing_manifest: bool = False,
    invalid_json: bool = False,
    missing_name: bool = False,
) -> Path:
    """Helper to create a plugin zip file."""
    zip_path = tmp_path / f"{name}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        if not missing_manifest:
            manifest = {}
            if not missing_name:
                manifest["name"] = name
            manifest["description"] = description
            manifest["version"] = version
            manifest["author"] = author

            content = "not json {{{{" if invalid_json else json.dumps(manifest)

            prefix = f"{name}/" if nested else ""
            zf.writestr(f"{prefix}.claude-plugin/plugin.json", content)

        if extra_files:
            for fname, fcontent in extra_files.items():
                prefix = f"{name}/" if nested else ""
                zf.writestr(f"{prefix}{fname}", fcontent)

    return zip_path


class TestValidatePluginDir:
    def test_valid_dir(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path)
        name, desc, version, author = validate_plugin_dir(plugin_dir)
        assert name == "test-plugin"
        assert desc == "A test plugin"
        assert version == "1.0.0"
        assert author == "Test Author"

    def test_missing_manifest(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, missing_manifest=True)
        with pytest.raises(ValueError, match=r"No \.claude-plugin/plugin\.json found"):
            validate_plugin_dir(plugin_dir)

    def test_invalid_json(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, invalid_json=True)
        with pytest.raises(ValueError, match=r"Invalid plugin\.json"):
            validate_plugin_dir(plugin_dir)

    def test_missing_name(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, missing_name=True)
        with pytest.raises(ValueError, match="missing required 'name'"):
            validate_plugin_dir(plugin_dir)

    def test_missing_description(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, missing_description=True)
        with pytest.raises(ValueError, match="missing required 'description'"):
            validate_plugin_dir(plugin_dir)

    def test_missing_version(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, missing_version=True)
        with pytest.raises(ValueError, match="missing required 'version'"):
            validate_plugin_dir(plugin_dir)

    def test_missing_author(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, missing_author=True)
        with pytest.raises(ValueError, match="missing required 'author'"):
            validate_plugin_dir(plugin_dir)

    def test_invalid_name(self, tmp_path):
        plugin_dir = _make_plugin_dir(tmp_path, name="BadName")
        with pytest.raises(ValueError, match="Invalid plugin name"):
            validate_plugin_dir(plugin_dir)

    def test_not_a_directory(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Not a directory"):
            validate_plugin_dir(tmp_path / "nope")


class TestValidatePluginZip:
    def test_valid_root_manifest(self, tmp_path):
        zip_path = _make_plugin_zip(tmp_path)
        name, desc, version, author, rel_dir = validate_plugin_zip(zip_path)
        assert name == "test-plugin"
        assert desc == "A test plugin"
        assert version == "1.0.0"
        assert author == "Test Author"
        assert rel_dir == ""

    def test_valid_nested_manifest(self, tmp_path):
        zip_path = _make_plugin_zip(tmp_path, nested=True)
        name, _desc, _version, _author, rel_dir = validate_plugin_zip(zip_path)
        assert name == "test-plugin"
        assert rel_dir == "test-plugin"

    def test_missing_manifest(self, tmp_path):
        zip_path = _make_plugin_zip(
            tmp_path, missing_manifest=True, extra_files={"readme.md": "hi"}
        )
        with pytest.raises(ValueError, match=r"No \.claude-plugin/plugin\.json found"):
            validate_plugin_zip(zip_path)

    def test_invalid_json_in_zip(self, tmp_path):
        zip_path = _make_plugin_zip(tmp_path, invalid_json=True)
        with pytest.raises(ValueError, match=r"Invalid plugin\.json"):
            validate_plugin_zip(zip_path)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            validate_plugin_zip(tmp_path / "nope.zip")

    def test_missing_name_in_zip(self, tmp_path):
        zip_path = _make_plugin_zip(tmp_path, missing_name=True)
        with pytest.raises(ValueError, match="missing required 'name'"):
            validate_plugin_zip(zip_path)


class TestInstallPlugin:
    def test_install_from_dir(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(
            tmp_path / "src", extra_files={"skills/helper.md": "# helper"}
        )
        plugin = install_plugin(plugin_dir)
        assert plugin.name == "test-plugin"
        assert plugin.description == "A test plugin"
        assert plugin.version == "1.0.0"
        assert plugin.author == "Test Author"
        installed_manifest = (
            fake_plugins_dir / "test-plugin" / ".claude-plugin" / "plugin.json"
        )
        assert installed_manifest.is_file()
        assert (fake_plugins_dir / "test-plugin" / "skills" / "helper.md").is_file()

    def test_install_from_zip(self, tmp_path, fake_config_dir, fake_plugins_dir):
        zip_path = _make_plugin_zip(
            tmp_path, extra_files={"agents/my-agent.py": "# agent"}
        )
        plugin = install_plugin(zip_path)
        assert plugin.name == "test-plugin"
        assert (
            fake_plugins_dir / "test-plugin" / ".claude-plugin" / "plugin.json"
        ).is_file()
        assert (fake_plugins_dir / "test-plugin" / "agents" / "my-agent.py").is_file()

    def test_overwrite_existing(self, tmp_path, fake_config_dir, fake_plugins_dir):
        dir1 = _make_plugin_dir(tmp_path / "v1", description="v1", version="1.0.0")
        install_plugin(dir1)
        assert get_plugin("test-plugin").description == "v1"

        dir2 = _make_plugin_dir(tmp_path / "v2", description="v2", version="2.0.0")
        install_plugin(dir2)
        assert get_plugin("test-plugin").description == "v2"
        assert get_plugin("test-plugin").version == "2.0.0"

    def test_invalid_source(self, tmp_path, fake_config_dir, fake_plugins_dir):
        with pytest.raises(ValueError, match=r"must be a directory or \.zip"):
            install_plugin(tmp_path / "nonexistent.txt")

    def test_install_nested_zip(self, tmp_path, fake_config_dir, fake_plugins_dir):
        zip_path = _make_plugin_zip(tmp_path, nested=True)
        plugin = install_plugin(zip_path)
        assert (
            fake_plugins_dir / "test-plugin" / ".claude-plugin" / "plugin.json"
        ).is_file()
        assert plugin.name == "test-plugin"


class TestRemovePlugin:
    def test_remove_existing(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        assert remove_plugin("test-plugin") is True
        assert not (fake_plugins_dir / "test-plugin").exists()
        assert get_plugin("test-plugin") is None

    def test_remove_nonexistent(self, fake_config_dir, fake_plugins_dir):
        assert remove_plugin("nope") is False


class TestListPlugins:
    def test_empty(self, fake_config_dir):
        assert list_plugins() == []

    def test_lists_installed(self, tmp_path, fake_config_dir, fake_plugins_dir):
        dir_a = _make_plugin_dir(
            tmp_path / "a", name="plugin-a", description="Plugin A"
        )
        dir_b = _make_plugin_dir(
            tmp_path / "b", name="plugin-b", description="Plugin B"
        )
        install_plugin(dir_a)
        install_plugin(dir_b)

        plugins = list_plugins()
        names = {p.name for p in plugins}
        assert names == {"plugin-a", "plugin-b"}


class TestEnableDisablePlugin:
    def test_enable_disabled(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        disable_plugin("test-plugin")
        assert get_plugin("test-plugin").enabled is False
        enable_plugin("test-plugin")
        assert get_plugin("test-plugin").enabled is True

    def test_disable_enabled(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        assert get_plugin("test-plugin").enabled is True
        disable_plugin("test-plugin")
        assert get_plugin("test-plugin").enabled is False

    def test_enable_nonexistent(self, fake_config_dir):
        assert enable_plugin("nope") is False

    def test_disable_nonexistent(self, fake_config_dir):
        assert disable_plugin("nope") is False


class TestGetEnabledPluginPaths:
    def test_returns_enabled(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        paths = get_enabled_plugin_paths()
        assert len(paths) == 1
        assert "test-plugin" in paths[0]

    def test_excludes_disabled(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        disable_plugin("test-plugin")
        paths = get_enabled_plugin_paths()
        assert paths == []

    def test_empty_when_none(self, fake_config_dir):
        paths = get_enabled_plugin_paths()
        assert paths == []

    def test_excludes_missing_dir(self, tmp_path, fake_config_dir, fake_plugins_dir):
        """Plugin in config but directory deleted from disk."""
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        # Remove the installed directory
        import shutil

        shutil.rmtree(fake_plugins_dir / "test-plugin")
        paths = get_enabled_plugin_paths()
        assert paths == []


class TestHasInstalledPlugins:
    def test_false_when_empty(self, fake_config_dir):
        assert has_installed_plugins() is False

    def test_true_when_installed(self, tmp_path, fake_config_dir, fake_plugins_dir):
        plugin_dir = _make_plugin_dir(tmp_path / "src")
        install_plugin(plugin_dir)
        assert has_installed_plugins() is True


class TestPluginInfo:
    def test_frozen(self):
        from pydantic import ValidationError

        info = PluginInfo(
            name="test",
            description="desc",
            version="1.0.0",
            author="Author",
            installed_at="2026-01-01",
            source="/tmp/test",
        )
        with pytest.raises(ValidationError, match="frozen"):
            info.name = "other"  # type: ignore[misc]

    def test_default_enabled(self):
        info = PluginInfo(
            name="test",
            description="desc",
            version="1.0.0",
            author="Author",
            installed_at="2026-01-01",
            source="/tmp/test",
        )
        assert info.enabled is True


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
    def test_remove_traversal_blocked(self, fake_config_dir, fake_plugins_dir):
        with pytest.raises(ValueError, match="Invalid plugin name"):
            remove_plugin("../../etc")

    def test_get_plugin_traversal_blocked(self, fake_config_dir):
        with pytest.raises(ValueError, match="Invalid plugin name"):
            get_plugin("../../etc")
