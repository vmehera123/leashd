"""Tests for project test config loader."""

import pytest

from leashd.plugins.builtin.test_config_loader import (
    ProjectTestConfig,
    discover_api_specs,
    load_project_test_config,
)
from leashd.plugins.builtin.test_runner import (
    TestConfig,
    merge_project_config,
)


class TestLoadProjectTestConfig:
    @pytest.fixture
    def leashd_dir(self, tmp_path):
        d = tmp_path / ".leashd"
        d.mkdir()
        return d

    def test_load_valid_yaml(self, tmp_path, leashd_dir):
        config_file = leashd_dir / "test.yaml"
        config_file.write_text(
            "url: http://localhost:3000\n"
            "server: npm run dev\n"
            "framework: next.js\n"
            "credentials:\n"
            "  api_token: abc123\n"
            "preconditions:\n"
            "  - Backend must be running\n"
            "focus_areas:\n"
            "  - SKU replacement\n"
        )
        result = load_project_test_config(str(tmp_path))
        assert result is not None
        assert result.url == "http://localhost:3000"
        assert result.server == "npm run dev"
        assert result.framework == "next.js"
        assert result.credentials == {"api_token": "abc123"}
        assert result.preconditions == ["Backend must be running"]
        assert result.focus_areas == ["SKU replacement"]

    def test_load_yml_extension(self, tmp_path, leashd_dir):
        config_file = leashd_dir / "test.yml"
        config_file.write_text("url: http://localhost:8080\n")
        result = load_project_test_config(str(tmp_path))
        assert result is not None
        assert result.url == "http://localhost:8080"

    def test_load_missing_file(self, tmp_path):
        result = load_project_test_config(str(tmp_path))
        assert result is None

    def test_load_invalid_yaml(self, tmp_path, leashd_dir):
        config_file = leashd_dir / "test.yaml"
        config_file.write_text("url: [invalid: yaml: {{")
        result = load_project_test_config(str(tmp_path))
        assert result is None

    def test_load_empty_file(self, tmp_path, leashd_dir):
        config_file = leashd_dir / "test.yaml"
        config_file.write_text("")
        result = load_project_test_config(str(tmp_path))
        assert result is not None
        assert result.url is None

    def test_load_partial_config(self, tmp_path, leashd_dir):
        config_file = leashd_dir / "test.yaml"
        config_file.write_text("framework: django\n")
        result = load_project_test_config(str(tmp_path))
        assert result is not None
        assert result.framework == "django"
        assert result.url is None
        assert result.credentials == {}

    def test_yaml_takes_precedence_over_yml(self, tmp_path, leashd_dir):
        (leashd_dir / "test.yaml").write_text("url: http://yaml\n")
        (leashd_dir / "test.yml").write_text("url: http://yml\n")
        result = load_project_test_config(str(tmp_path))
        assert result is not None
        assert result.url == "http://yaml"


class TestProjectTestConfigModel:
    def test_defaults(self):
        c = ProjectTestConfig()
        assert c.url is None
        assert c.server is None
        assert c.framework is None
        assert c.directory is None
        assert c.credentials == {}
        assert c.preconditions == []
        assert c.focus_areas == []
        assert c.environment == {}
        assert c.api_specs == []

    def test_frozen(self):
        from pydantic import ValidationError

        c = ProjectTestConfig(url="http://localhost")
        with pytest.raises(ValidationError, match="frozen"):
            c.url = "http://other"  # type: ignore[misc]


class TestMergeProjectConfig:
    def test_cli_overrides_project(self):
        cli = TestConfig(app_url="http://cli")
        project = ProjectTestConfig(url="http://project")
        merged = merge_project_config(cli, project)
        assert merged.app_url == "http://cli"

    def test_project_fills_gaps(self):
        cli = TestConfig()
        project = ProjectTestConfig(
            url="http://project",
            server="npm run dev",
            framework="next.js",
            directory="tests/e2e",
        )
        merged = merge_project_config(cli, project)
        assert merged.app_url == "http://project"
        assert merged.dev_server_command == "npm run dev"
        assert merged.framework == "next.js"
        assert merged.test_directory == "tests/e2e"

    def test_partial_merge(self):
        cli = TestConfig(framework="react")
        project = ProjectTestConfig(url="http://project", framework="next.js")
        merged = merge_project_config(cli, project)
        assert merged.app_url == "http://project"
        assert merged.framework == "react"  # CLI wins

    def test_no_updates_returns_same(self):
        cli = TestConfig(
            app_url="http://cli",
            dev_server_command="npm start",
            framework="react",
            test_directory="tests/",
        )
        project = ProjectTestConfig()
        merged = merge_project_config(cli, project)
        assert merged is cli  # No copy needed


class TestDiscoverApiSpecs:
    def test_finds_http_files(self, tmp_path):
        requests_dir = tmp_path / "requests"
        requests_dir.mkdir()
        http_file = requests_dir / "localhost.http"
        http_file.write_text("GET http://localhost:8080/api/health\n")
        result = discover_api_specs(str(tmp_path))
        assert len(result) == 1
        assert result[0][0] == "requests/localhost.http"
        assert "GET http://localhost:8080" in result[0][1]

    def test_skips_excluded_dirs(self, tmp_path):
        nm = tmp_path / "node_modules" / "some_pkg"
        nm.mkdir(parents=True)
        (nm / "api.http").write_text("GET /ignored\n")
        result = discover_api_specs(str(tmp_path))
        assert len(result) == 0

    def test_truncates_large_files(self, tmp_path):
        big = tmp_path / "api.http"
        big.write_text("x" * 5000)
        result = discover_api_specs(str(tmp_path))
        assert len(result) == 1
        assert len(result[0][1]) == 2000

    def test_empty_project(self, tmp_path):
        result = discover_api_specs(str(tmp_path))
        assert result == []

    def test_explicit_paths_override_discovery(self, tmp_path):
        # Auto-discoverable file that should be ignored
        (tmp_path / "auto.http").write_text("auto content")
        # Explicit file
        subdir = tmp_path / "docs"
        subdir.mkdir()
        (subdir / "spec.yaml").write_text("openapi: 3.0.0")
        result = discover_api_specs(str(tmp_path), explicit_paths=["docs/spec.yaml"])
        assert len(result) == 1
        assert result[0][0] == "docs/spec.yaml"
        assert "openapi" in result[0][1]

    def test_finds_openapi_files(self, tmp_path):
        (tmp_path / "openapi.yaml").write_text("openapi: 3.0.0")
        (tmp_path / "swagger.json").write_text('{"swagger": "2.0"}')
        result = discover_api_specs(str(tmp_path))
        paths = {r[0] for r in result}
        assert "openapi.yaml" in paths
        assert "swagger.json" in paths

    def test_respects_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "api.http").write_text("too deep")
        result = discover_api_specs(str(tmp_path))
        assert len(result) == 0

    def test_explicit_missing_file_skipped(self, tmp_path):
        result = discover_api_specs(str(tmp_path), explicit_paths=["nonexistent.http"])
        assert result == []
