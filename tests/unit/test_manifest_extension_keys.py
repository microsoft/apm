"""x-* manifest extensions are kept and other unknown keys warn (#3219).

Spec bind: tests/spec_conformance/test_manifest_extension_reqs.py (req-ext-003).
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.installed_package import InstalledPackage, extensions_from_resolved_package
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin
from apm_cli.models.apm_package import (
    _KNOWN_TOP_LEVEL_KEYS,
    APMPackage,
    DependencyReference,
    _collect_manifest_extensions,
    _warn_unknown_manifest_keys,
    clear_apm_yml_cache,
)

_WARN_PATH = "apm_cli.utils.console._rich_warning"


def _write(path: Path, text: str) -> Path:
    clear_apm_yml_cache()
    path.write_text(text, encoding="utf-8")
    return path


class TestParsedExtensions:
    def test_file_preserves_declaration_order(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "apm.yml",
            "name: pkg\nversion: 1.0.0\nx-zeta: 1\nx-alpha:\n  nested: keep\nx-flag: true\n",
        )

        pkg = APMPackage.from_apm_yml(path, create_config=False)

        assert list(pkg.extensions) == ["x-zeta", "x-alpha", "x-flag"]
        assert pkg.extensions["x-zeta"] == 1
        assert pkg.extensions["x-alpha"] == {"nested": "keep"}
        assert pkg.extensions["x-flag"] is True

    def test_unknown_key_warns_by_name_and_does_not_fail(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "apm.yml",
            "name: pkg\nversion: 1.0.0\nnot-a-field: 1\nalso-unknown: 2\nx-keep: kept\n",
        )

        with patch(_WARN_PATH) as mock_warn:
            pkg = APMPackage.from_apm_yml(path, create_config=False)

        assert pkg.name == "pkg"
        assert pkg.extensions == {"x-keep": "kept"}
        mock_warn.assert_called_once()
        message = mock_warn.call_args[0][0]
        assert "not-a-field" in message
        assert "also-unknown" in message
        assert "x-keep" not in message

    def test_known_keys_produce_no_warning(self, tmp_path: Path) -> None:
        data = {key: "ignored-by-helper" for key in _KNOWN_TOP_LEVEL_KEYS}

        with patch(_WARN_PATH) as mock_warn:
            extensions = _collect_manifest_extensions(data, tmp_path / "apm.yml")

        mock_warn.assert_not_called()
        assert extensions == {}

    def test_valid_known_manifest_is_silent(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "apm.yml",
            "name: pkg\n"
            "version: 1.0.0\n"
            "description: d\n"
            "author: a\n"
            "license: MIT\n"
            "homepage: https://example.com\n"
            "repository: https://example.com/repo\n"
            "keywords:\n"
            "  - one\n"
            "tags:\n"
            "  - alpha\n"
            "build: {}\n"
            "manifestVersion: '1'\n"
            "schemaVersion: '1'\n"
            "target: copilot\n",
        )

        with patch(_WARN_PATH) as mock_warn:
            pkg = APMPackage.from_apm_yml(path, create_config=False)

        mock_warn.assert_not_called()
        assert pkg.extensions == {}

    def test_tags_metadata_does_not_warn(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "apm.yml",
            "name: pkg\nversion: 1.0.0\ntags:\n  - alpha\n  - beta\n",
        )

        with patch(_WARN_PATH) as mock_warn:
            pkg = APMPackage.from_apm_yml(path, create_config=False)

        mock_warn.assert_not_called()
        assert pkg.extensions == {}
        assert "tags" in _KNOWN_TOP_LEVEL_KEYS

    def test_synthesized_plugin_manifest_is_silent(self, tmp_path: Path) -> None:
        path = synthesize_apm_yml_from_plugin(
            tmp_path,
            {"name": "plugin", "version": "1.0.0", "tags": ["alpha", "beta"]},
        )
        assert "tags:" in path.read_text(encoding="utf-8")
        with patch(_WARN_PATH) as warned:
            package = APMPackage.from_apm_yml(path, create_config=False)

        assert package.name == "plugin"
        assert package.extensions == {}
        warned.assert_not_called()

    @pytest.mark.windows_compat
    def test_windows_warning_path_is_portable_and_ascii(self) -> None:
        path = PureWindowsPath("C:/Users/caf\u00e9/project/apm.yml")
        with patch(_WARN_PATH) as warned:
            _warn_unknown_manifest_keys(["unknown"], path)

        message = warned.call_args[0][0]
        assert "C:/Users/caf\\xe9/project/apm.yml" in message
        assert "\\\\" not in message
        assert message.isascii()

    def test_custom_warning_handler_preserves_extensions(self, tmp_path: Path) -> None:
        handler = Mock()
        with patch(_WARN_PATH) as terminal_warning:
            package = APMPackage.from_mapping(
                {"name": "pkg", "version": "1", "unknown": True, "x-team": "platform"},
                package_path=tmp_path,
                create_config=False,
                warning_handler=handler,
            )

        assert package.extensions == {"x-team": "platform"}
        handler.assert_called_once_with(["unknown"], tmp_path / "apm.yml")
        terminal_warning.assert_not_called()

    def test_top_level_source_warns_as_unknown(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "apm.yml",
            "name: pkg\nversion: 1.0.0\nsource: owner/repo\n",
        )

        with patch(_WARN_PATH) as mock_warn:
            pkg = APMPackage.from_apm_yml(path, create_config=False)

        assert pkg.source is None
        assert pkg.extensions == {}
        assert "source" not in _KNOWN_TOP_LEVEL_KEYS
        mock_warn.assert_called_once()
        message = mock_warn.call_args[0][0]
        assert ": source. These keys were ignored." in message

    def test_invalid_x_prefix_forms_warn_and_are_not_kept(self, tmp_path: Path) -> None:
        data = {
            "name": "pkg",
            "version": "1.0.0",
            "x-": 1,
            "x-Foo": 2,
            "x-a_b": 3,
            "x-keep": "kept",
        }

        with patch(_WARN_PATH) as mock_warn:
            pkg = APMPackage.from_mapping(
                data,
                package_path=tmp_path,
                create_config=False,
            )

        assert pkg.extensions == {"x-keep": "kept"}
        mock_warn.assert_called_once()
        message = mock_warn.call_args[0][0]
        assert "x-, x-Foo, x-a_b" in message
        assert "x-keep" not in message

    def test_non_string_key_warns_without_raising(self, tmp_path: Path) -> None:
        data: dict[object, object] = {True: 1, "name": "pkg", "version": "1.0.0"}

        with patch(_WARN_PATH) as mock_warn:
            pkg = APMPackage.from_mapping(
                data,  # type: ignore[arg-type]
                package_path=tmp_path,
                create_config=False,
            )

        assert pkg.extensions == {}
        mock_warn.assert_called_once()
        assert "True" in mock_warn.call_args[0][0]

    def test_non_ascii_unknown_key_is_escaped(self, tmp_path: Path) -> None:
        key = "caf\u00e9"
        path = _write(
            tmp_path / "apm.yml",
            "name: pkg\nversion: 1.0.0\n" + key + ": 1\n",
        )

        with patch(_WARN_PATH) as mock_warn:
            APMPackage.from_apm_yml(path, create_config=False)

        message = mock_warn.call_args[0][0]
        assert "caf\\xe9" in message
        assert key not in message


class TestLockfileExtensions:
    def test_yaml_round_trip_preserves_order(self) -> None:
        dep = LockedDependency(
            repo_url="owner/repo",
            resolved_commit="a" * 40,
            extensions={"x-zeta": 1, "x-alpha": {"nested": "keep"}},
        )

        text = LockFile(dependencies={"owner/repo": dep}).to_yaml()

        assert "extensions:" in text
        assert text.index("x-zeta:") < text.index("x-alpha:")
        restored = LockFile.from_yaml(text).get_package_dependencies()[0]
        assert list(restored.extensions) == ["x-zeta", "x-alpha"]
        assert restored.extensions["x-alpha"] == {"nested": "keep"}
        assert "extensions" not in restored._unknown_fields

    def test_empty_extensions_omitted(self) -> None:
        dep = LockedDependency(repo_url="owner/repo", extensions={})

        assert "extensions" not in dep.to_dict()
        text = LockFile(dependencies={"owner/repo": dep}).to_yaml()
        assert "extensions:" not in text

    def test_from_installed_packages_carries_extensions(self) -> None:
        entry = InstalledPackage(
            dep_ref=DependencyReference(repo_url="owner/repo", reference="main"),
            resolved_commit="abc",
            depth=1,
            resolved_by=None,
            extensions={"x-team": "platform"},
        )

        locked = LockFile.from_installed_packages([entry], None).get_package_dependencies()[0]

        assert locked.extensions == {"x-team": "platform"}
        assert "extensions" in locked.to_dict()

    def test_from_installed_packages_omits_when_empty(self) -> None:
        entry = InstalledPackage(
            dep_ref=DependencyReference(repo_url="owner/repo", reference="main"),
            resolved_commit="abc",
            depth=1,
            resolved_by=None,
        )

        locked = LockFile.from_installed_packages([entry], None).get_package_dependencies()[0]

        assert locked.extensions == {}
        assert "extensions" not in locked.to_dict()

    def test_non_mapping_extensions_do_not_raise(self) -> None:
        raw = {"repo_url": "owner/repo", "extensions": "nope"}

        dep = LockedDependency.from_dict(raw)

        assert dep.extensions == {}
        assert "extensions" not in dep._unknown_fields

    def test_from_dict_copies_the_mapping(self) -> None:
        raw = {"repo_url": "owner/repo", "extensions": {"x-a": 1}}

        dep = LockedDependency.from_dict(raw)
        raw["extensions"]["x-b"] = 2

        assert list(dep.extensions) == ["x-a"]

    def test_helper_copies_package_extensions(self) -> None:
        class _Package:
            def __init__(self) -> None:
                self.extensions = {"x-a": 1}

        package = _Package()
        copied = extensions_from_resolved_package(package)
        copied["x-b"] = 2

        assert package.extensions == {"x-a": 1}
        assert extensions_from_resolved_package(None) == {}


class TestApmLockRecordsDependencyExtensions:
    def test_install_dry_run_warns_once_per_invocation(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            project = Path.cwd()
            root = _write(
                project / "apm.yml",
                "name: project\nversion: 1.0.0\nroot-unknown: true\nx-team: platform\n",
            )
            package = project / "sub"
            package.mkdir()
            _write(
                package / "apm.yml",
                "name: sub\nversion: 1.0.0\n",
            )
            instructions = package / ".apm" / "instructions"
            instructions.mkdir(parents=True)
            (instructions / "test.instructions.md").write_text(
                "---\napplyTo: '**'\n---\n# Test\n", encoding="utf-8"
            )
            original = root.read_bytes()

            for _ in range(2):
                clear_apm_yml_cache()
                result = runner.invoke(cli, ["install", "./sub", "--dry-run", "--no-policy"])

                assert result.exit_code == 0, result.output
                assert result.output.count(": root-unknown.") == 1, result.output
                assert root.read_bytes() == original
                assert not (project / "apm.lock.yaml").exists()
                assert not (project / "apm_modules").exists()

    def test_lock_writes_dependency_extensions(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            project = Path.cwd()
            package = project / "vendor" / "my-skills"
            package.mkdir(parents=True)
            (package / "apm.yml").write_text(
                "name: my-skills\n"
                "version: 1.0.0\n"
                "description: local package\n"
                "x-zeta: 1\n"
                "x-alpha:\n"
                "  nested: keep\n"
                "not-a-field: dropped\n",
                encoding="utf-8",
            )
            instructions = package / ".apm" / "instructions"
            instructions.mkdir(parents=True)
            (instructions / "test-skill.instructions.md").write_text(
                "---\napplyTo: '**'\n---\n# Test\n",
                encoding="utf-8",
            )
            quoted = str(package).replace("\\", "\\\\")
            (project / "apm.yml").write_text(
                "name: lock-test-project\n"
                "version: 1.0.0\n"
                "x-root: kept\n"
                "dependencies:\n"
                "  apm:\n"
                f'    - "{quoted}"\n',
                encoding="utf-8",
            )
            clear_apm_yml_cache()

            result = runner.invoke(cli, ["lock", "--no-policy"])

            assert result.exit_code == 0, result.output
            assert "not-a-field" in result.output
            root = APMPackage.from_apm_yml(project / "apm.yml", create_config=False)
            assert root.extensions == {"x-root": "kept"}
            lock_text = (project / "apm.lock.yaml").read_text(encoding="utf-8")
            assert "extensions:" in lock_text
            assert lock_text.index("x-zeta:") < lock_text.index("x-alpha:")
            locked = LockFile.read(project / "apm.lock.yaml")
            assert locked is not None
            deps = locked.get_package_dependencies()
            assert len(deps) == 1
            assert deps[0].extensions == {"x-zeta": 1, "x-alpha": {"nested": "keep"}}
            assert "not-a-field" not in deps[0].extensions
