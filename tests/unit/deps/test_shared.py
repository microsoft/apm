"""Unit tests for apm_cli.deps._shared."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps._shared import (
    MarketplaceManifestMaterializationError,
    _validate_and_load_package,
    materialize_marketplace_manifest,
)
from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.models.validation import validate_apm_package
from apm_cli.utils.content_hash import compute_package_hash

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(repo_url: str = "github.com/owner/repo") -> MagicMock:
    dep_ref = MagicMock()
    dep_ref.repo_url = repo_url
    dep_ref.to_github_url.return_value = f"https://{repo_url}"
    return dep_ref


def _make_valid_result(package: MagicMock) -> MagicMock:
    result = MagicMock()
    result.is_valid = True
    result.package = package
    result.errors = []
    return result


def _make_invalid_result(errors: list[str]) -> MagicMock:
    result = MagicMock()
    result.is_valid = False
    result.package = None
    result.errors = errors
    return result


# ---------------------------------------------------------------------------
# _validate_and_load_package
# ---------------------------------------------------------------------------


class TestValidateAndLoadPackage:
    """Tests for _validate_and_load_package."""

    def test_returns_package_on_success(self, tmp_path: Path) -> None:
        package = MagicMock()
        package.source = None
        dep_ref = _make_dep_ref()
        validation_result = _make_valid_result(package)

        result = _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert result is package

    def test_sets_package_source_to_github_url(self, tmp_path: Path) -> None:
        package = MagicMock()
        package.source = None
        dep_ref = _make_dep_ref()
        dep_ref.to_github_url.return_value = "https://github.com/owner/repo"
        validation_result = _make_valid_result(package)

        _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert package.source == "https://github.com/owner/repo"

    def test_materializes_metadata_only_marketplace_plugin(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("catalog-only plugin")
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "gopls-lsp",
            "lspServers": {
                "gopls": {
                    "command": "gopls",
                    "extensionToLanguage": {".go": "go"},
                }
            },
        }

        package = _validate_and_load_package(validate_apm_package(tmp_path), tmp_path, dep_ref)

        assert [dep.name for dep in package.get_lsp_dependencies()] == ["gopls"]
        assert (tmp_path / "apm.yml").is_file()

    def test_materializes_metadata_only_marketplace_mcp_plugin(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("catalog-only plugin")
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "test-mcp",
            "mcpServers": {"test-server": {"command": "echo", "args": ["hello"]}},
        }

        package = _validate_and_load_package(validate_apm_package(tmp_path), tmp_path, dep_ref)

        assert [dep.name for dep in package.get_mcp_dependencies()] == ["test-server"]
        assert (tmp_path / "apm.yml").is_file()

    def test_rejects_invalid_marketplace_metadata_and_cleans_target(self, tmp_path: Path) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        (target / "README.md").write_text("catalog-only plugin")
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "bad-lsp",
            "lspServers": {"gopls": {"command": "gopls"}},
        }

        with pytest.raises(ValueError) as exc_info:
            _validate_and_load_package(validate_apm_package(target), target, dep_ref)

        assert "invalid marketplace metadata for 'plugins'" in str(exc_info.value)
        assert not target.exists()

    def test_rejects_invalid_marketplace_mcp_metadata_and_cleans_target(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        (target / "README.md").write_text("catalog-only plugin")
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "bad-mcp",
            "mcpServers": {"test-server": {"args": ["hello"]}},
        }

        with pytest.raises(ValueError, match="did not materialize"):
            _validate_and_load_package(validate_apm_package(target), target, dep_ref)

        assert not target.exists()

    @pytest.mark.parametrize(
        ("field", "valid", "invalid"),
        [
            (
                "lspServers",
                {"command": "gopls", "extensionToLanguage": {".go": "go"}},
                {"command": "gopls"},
            ),
            (
                "mcpServers",
                {"command": "echo", "args": ["ok"]},
                {"args": ["missing-command"]},
            ),
        ],
    )
    def test_rejects_partially_invalid_marketplace_metadata(
        self,
        tmp_path: Path,
        field: str,
        valid: dict[str, object],
        invalid: dict[str, object],
    ) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "mixed-plugin",
            field: {"valid": valid, "invalid": invalid},
        }

        with pytest.raises(
            MarketplaceManifestMaterializationError,
            match="did not materialize every declared server: invalid",
        ):
            materialize_marketplace_manifest(dep_ref, target)

        assert not target.exists()

    def test_rejects_dangling_apm_yml_symlink_without_writing_outside(self, tmp_path: Path) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        outside = tmp_path / "outside.yml"
        try:
            (target / "apm.yml").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks are unavailable")
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "catalog-only",
            "mcpServers": {"server": {"command": "echo"}},
        }

        with pytest.raises(
            MarketplaceManifestMaterializationError,
            match="must not be symbolic links",
        ):
            materialize_marketplace_manifest(dep_ref, target)

        assert not outside.exists()
        assert not target.exists()

    def test_rematerializes_when_catalog_manifest_variant_changes(self, tmp_path: Path) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "catalog-only",
            "mcpServers": {"first": {"command": "echo"}},
        }

        assert materialize_marketplace_manifest(dep_ref, target)
        dep_ref.marketplace_manifest = {
            "name": "catalog-only",
            "mcpServers": {"second": {"command": "echo", "args": ["second"]}},
        }

        assert materialize_marketplace_manifest(dep_ref, target)
        package = validate_apm_package(target).package
        assert package is not None
        assert [dependency.name for dependency in package.get_mcp_dependencies()] == ["second"]

    def test_repairs_tampered_generated_manifest_before_reuse(self, tmp_path: Path) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "catalog-only",
            "mcpServers": {"server": {"command": "echo"}},
        }
        assert materialize_marketplace_manifest(dep_ref, target)
        with (target / "apm.yml").open("a", encoding="utf-8") as manifest_file:
            manifest_file.write("\ndependencies:\n  apm:\n    - attacker/injected\n")

        assert materialize_marketplace_manifest(dep_ref, target)
        package = validate_apm_package(target).package
        assert package is not None
        assert package.get_apm_dependencies() == []

    @pytest.mark.windows_compat
    def test_catalog_manifest_bytes_are_independent_of_checkout_root(self, tmp_path: Path) -> None:
        targets = [tmp_path / root / "plugin" for root in ("first", "second")]
        manifest = {
            "name": "portable",
            "mcpServers": {
                "server": {
                    "command": "${CLAUDE_PLUGIN_ROOT}/bin/server",
                    "args": ["--root", "${CLAUDE_PLUGIN_ROOT}"],
                }
            },
        }
        loaded_commands: list[Path] = []
        for target in targets:
            target.mkdir(parents=True)
            (target / "README.md").write_text("portable catalog package")
            dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
            dep_ref.marketplace_manifest = manifest
            assert materialize_marketplace_manifest(dep_ref, target)
            package = APMPackage.from_apm_yml(target / "apm.yml")
            command = package.get_mcp_dependencies()[0].command
            assert command is not None
            loaded_commands.append(Path(command))

        assert (targets[0] / "apm.yml").read_bytes() == (targets[1] / "apm.yml").read_bytes()
        assert compute_package_hash(targets[0]) == compute_package_hash(targets[1])
        assert loaded_commands == [
            targets[0].resolve() / "bin" / "server",
            targets[1].resolve() / "bin" / "server",
        ]

    def test_cleanup_failure_cannot_leave_generated_manifest(self, tmp_path: Path) -> None:
        target = tmp_path / "plugin"
        target.mkdir()
        dep_ref = DependencyReference(repo_url="owner/plugins", is_virtual=True)
        dep_ref.marketplace_manifest = {
            "name": "bad-plugin",
            "mcpServers": {"invalid": {"args": ["missing-command"]}},
        }

        with (
            patch("apm_cli.utils.file_ops.robust_rmtree", side_effect=OSError("locked")),
            pytest.raises(
                MarketplaceManifestMaterializationError,
                match="rejected download cleanup also failed",
            ),
        ):
            materialize_marketplace_manifest(dep_ref, target)

        assert not (target / "apm.yml").exists()
        assert not (target / ".apm-marketplace-stage.yml").exists()

    def test_raises_on_invalid_result(self, tmp_path: Path) -> None:
        dep_ref = _make_dep_ref("github.com/owner/bad-pkg")
        validation_result = _make_invalid_result(["Missing apm.yml", "Invalid structure"])

        with pytest.raises(RuntimeError, match="Invalid APM package"):
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

    def test_error_message_contains_errors(self, tmp_path: Path) -> None:
        dep_ref = _make_dep_ref("github.com/o/r")
        validation_result = _make_invalid_result(["Error one", "Error two"])

        with pytest.raises(RuntimeError) as exc_info:
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert "Error one" in str(exc_info.value)
        assert "Error two" in str(exc_info.value)

    def test_removes_target_path_on_invalid_result(self, tmp_path: Path) -> None:
        # Create target directory that should be cleaned up
        target = tmp_path / "pkg"
        target.mkdir()
        (target / "file.txt").write_text("content", encoding="utf-8")

        dep_ref = _make_dep_ref()
        validation_result = _make_invalid_result(["Bad"])

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            with pytest.raises(RuntimeError):
                _validate_and_load_package(validation_result, target, dep_ref)

        mock_rmtree.assert_called_once_with(target, ignore_errors=True)

    def test_does_not_remove_path_when_not_exists(self, tmp_path: Path) -> None:
        # Target path does not exist
        target = tmp_path / "nonexistent"
        dep_ref = _make_dep_ref()
        validation_result = _make_invalid_result(["Bad"])

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            with pytest.raises(RuntimeError):
                _validate_and_load_package(validation_result, target, dep_ref)

        mock_rmtree.assert_not_called()

    def test_raises_when_valid_but_no_package(self, tmp_path: Path) -> None:
        result = MagicMock()
        result.is_valid = True
        result.package = None
        dep_ref = _make_dep_ref("github.com/owner/empty")

        with pytest.raises(RuntimeError, match="no package metadata found"):
            _validate_and_load_package(result, tmp_path, dep_ref)

    def test_error_message_contains_repo_url(self, tmp_path: Path) -> None:
        dep_ref = _make_dep_ref("github.com/owner/repo")
        validation_result = _make_invalid_result(["Bad"])

        with pytest.raises(RuntimeError) as exc_info:
            _validate_and_load_package(validation_result, tmp_path, dep_ref)

        assert "github.com/owner/repo" in str(exc_info.value)

    def test_removes_target_path_on_rejected_agent_plugin(self, tmp_path: Path) -> None:
        """A rejected Agent Plugin (e.g. unsupported schema) cleans up target_path.

        Regression test for the fail-open gap where route_agent_plugin_package()
        raising an AgentPluginError skipped cleanup entirely, even though the
        docstring promises target_path is removed on failure.
        """
        from apm_cli.agent_plugins.errors import AgentPluginManifestError

        target = tmp_path / "pkg"
        target.mkdir()
        (target / "plugin.json").write_text("{}", encoding="utf-8")
        dep_ref = _make_dep_ref("github.com/owner/rejected-plugin")
        validation_result = _make_valid_result(MagicMock())

        with (
            patch(
                "apm_cli.bundle.local_bundle.route_agent_plugin_package",
                side_effect=AgentPluginManifestError("unsupported schema"),
            ),
            patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree,
        ):
            with pytest.raises(AgentPluginManifestError):
                _validate_and_load_package(validation_result, target, dep_ref)

        mock_rmtree.assert_called_once_with(target, ignore_errors=True)
