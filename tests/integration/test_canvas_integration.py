"""Integration tests for canvas extension deploy pipeline.

Covers:
- Canvas discovery + deploy through integrate_canvases_for_target
- Trust gate enforcement (dependency vs first-party)
- Global scope refusal for non-default COPILOT_HOME
- _canvas_deploy_prefixes drift exclusion
- Canvas bundle path blocking in integrate_local_bundle
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.drift import _canvas_deploy_prefixes
from apm_cli.integration.canvas_integrator import (
    CanvasIntegrator,
    is_canvas_bundle_path,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_canvas_bundle(base: Path, name: str = "my-canvas") -> Path:
    """Create a minimal canvas bundle under .apm/extensions/<name>/."""
    bundle = base / ".apm" / "extensions" / name
    bundle.mkdir(parents=True)
    (bundle / "extension.mjs").write_text("export default {}")
    return bundle


def _make_target(
    name: str = "copilot",
    root_dir: str = ".github",
    deploy_root: str | None = None,
    subdir: str = "extensions",
) -> MagicMock:
    target = MagicMock()
    target.name = name
    target.root_dir = root_dir
    target.primitives = {
        "canvas": SimpleNamespace(
            deploy_root=deploy_root,
            subdir=subdir,
            format_id="plain",
            output_compare=False,
        )
    }
    return target


def _make_package_info(install_path: Path) -> MagicMock:
    pkg = MagicMock()
    pkg.install_path = str(install_path)
    pkg.package = MagicMock()
    pkg.package.name = "test-canvas-pkg"
    return pkg


# ------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------


class TestCanvasBundleDiscovery:
    def test_finds_valid_bundle(self, tmp_path: Path) -> None:
        _make_canvas_bundle(tmp_path, "demo-canvas")
        bundles = CanvasIntegrator.find_canvas_bundles(tmp_path)
        assert len(bundles) == 1
        assert bundles[0].name == "demo-canvas"

    def test_ignores_missing_entry_file(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / ".apm" / "extensions" / "no-entry"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "readme.md").write_text("not a canvas")
        assert CanvasIntegrator.find_canvas_bundles(tmp_path) == []

    def test_ignores_symlinked_bundle(self, tmp_path: Path) -> None:
        real = tmp_path / "real-bundle"
        real.mkdir()
        (real / "extension.mjs").write_text("export default {}")
        ext_dir = tmp_path / ".apm" / "extensions"
        ext_dir.mkdir(parents=True)
        (ext_dir / "symlinked").symlink_to(real)
        assert CanvasIntegrator.find_canvas_bundles(tmp_path) == []

    def test_finds_multiple_bundles_sorted(self, tmp_path: Path) -> None:
        _make_canvas_bundle(tmp_path, "zeta-canvas")
        _make_canvas_bundle(tmp_path, "alpha-canvas")
        bundles = CanvasIntegrator.find_canvas_bundles(tmp_path)
        assert [b.name for b in bundles] == ["alpha-canvas", "zeta-canvas"]

    def test_no_extensions_dir_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".apm").mkdir()
        assert CanvasIntegrator.find_canvas_bundles(tmp_path) == []


# ------------------------------------------------------------------
# Deploy pipeline (integrate_canvases_for_target)
# ------------------------------------------------------------------


class TestCanvasDeployPipeline:
    def test_first_party_deploys_without_trust_flag(self, tmp_path: Path) -> None:
        """First-party canvas deploys when experimental flag is on, no trust needed."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "my-widget")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=True,
                trust_canvas=False,
                package_name="root-pkg",
            )

        assert result.files_integrated == 1
        deployed = project_root / ".github" / "extensions" / "my-widget" / "extension.mjs"
        assert deployed.exists()

    def test_dependency_blocked_without_trust_flag(self, tmp_path: Path) -> None:
        """Dependency canvas is blocked when trust_canvas=False."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "dep-widget")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()
        diagnostics = MagicMock()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=False,
                trust_canvas=False,
                diagnostics=diagnostics,
                package_name="dep-pkg",
            )

        assert result.files_integrated == 0
        deployed = project_root / ".github" / "extensions" / "dep-widget"
        assert not deployed.exists()

    def test_dependency_deploys_with_trust_flag(self, tmp_path: Path) -> None:
        """Dependency canvas deploys when trust_canvas=True."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "dep-widget")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=False,
                trust_canvas=True,
                package_name="dep-pkg",
            )

        assert result.files_integrated == 1
        deployed = project_root / ".github" / "extensions" / "dep-widget" / "extension.mjs"
        assert deployed.exists()

    def test_experimental_flag_off_returns_empty(self, tmp_path: Path) -> None:
        """Canvas returns empty result when experimental flag is off."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "gated-canvas")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=False):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=True,
                trust_canvas=True,
                package_name="pkg",
            )

        assert result.files_integrated == 0

    def test_non_copilot_target_returns_empty(self, tmp_path: Path) -> None:
        """Canvas only deploys for copilot target."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "any-canvas")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target(name="claude")
        integrator = CanvasIntegrator()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=True,
                trust_canvas=True,
                package_name="pkg",
            )

        assert result.files_integrated == 0

    def test_deploy_creates_all_bundle_files(self, tmp_path: Path) -> None:
        """Deploy copies all files from the bundle, not just extension.mjs."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        bundle = _make_canvas_bundle(pkg_dir, "rich-canvas")
        (bundle / "style.css").write_text("body {}")
        (bundle / "lib").mkdir()
        (bundle / "lib" / "helper.mjs").write_text("export function x(){}")

        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=True,
                trust_canvas=True,
                package_name="pkg",
            )

        assert result.files_integrated == 1
        deploy_dir = project_root / ".github" / "extensions" / "rich-canvas"
        assert (deploy_dir / "extension.mjs").exists()
        assert (deploy_dir / "style.css").exists()
        assert (deploy_dir / "lib" / "helper.mjs").exists()


# ------------------------------------------------------------------
# Global scope guards
# ------------------------------------------------------------------


class TestGlobalCanvasGuards:
    def test_global_first_party_refused(self, tmp_path: Path) -> None:
        """First-party canvas is refused at user (global) scope."""
        from apm_cli.core.scope import InstallScope

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "local-canvas")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()
        diagnostics = MagicMock()

        with patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=True,
                trust_canvas=True,
                scope=InstallScope.USER,
                diagnostics=diagnostics,
                package_name="local-pkg",
            )

        assert result.files_integrated == 0

    def test_global_nondefault_copilot_home_refused(self, tmp_path: Path) -> None:
        """Non-default COPILOT_HOME refuses global canvas deploy."""
        from apm_cli.core.scope import InstallScope

        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        _make_canvas_bundle(pkg_dir, "dep-canvas")
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        integrator = CanvasIntegrator()
        diagnostics = MagicMock()

        with (
            patch("apm_cli.integration.canvas_integrator.is_enabled", return_value=True),
            patch.dict("os.environ", {"COPILOT_HOME": "/custom/path"}),
            patch.object(integrator, "_copilot_home_is_nondefault", return_value=True),
        ):
            result = integrator.integrate_canvases_for_target(
                target,
                _make_package_info(pkg_dir),
                project_root,
                force=False,
                is_first_party=False,
                trust_canvas=True,
                scope=InstallScope.USER,
                diagnostics=diagnostics,
                package_name="dep-pkg",
            )

        assert result.files_integrated == 0


# ------------------------------------------------------------------
# Drift exclusion: _canvas_deploy_prefixes
# ------------------------------------------------------------------


class TestCanvasDeployPrefixes:
    def test_no_targets_returns_empty(self) -> None:
        assert _canvas_deploy_prefixes(None) == set()
        assert _canvas_deploy_prefixes([]) == set()

    def test_target_without_canvas_mapping_ignored(self) -> None:
        target = MagicMock()
        target.primitives = {"prompts": SimpleNamespace(deploy_root=None, subdir="")}
        assert _canvas_deploy_prefixes([target]) == set()

    def test_target_with_canvas_mapping_returns_prefix(self) -> None:
        target = _make_target(root_dir=".github", subdir="extensions")
        prefixes = _canvas_deploy_prefixes([target])
        assert prefixes == {".github/extensions/"}

    def test_canvas_mapping_with_custom_deploy_root(self) -> None:
        target = _make_target(deploy_root=".copilot", subdir="extensions")
        prefixes = _canvas_deploy_prefixes([target])
        assert prefixes == {".copilot/extensions/"}

    def test_canvas_mapping_without_subdir(self) -> None:
        target = _make_target(root_dir=".github", subdir="")
        prefixes = _canvas_deploy_prefixes([target])
        assert prefixes == {".github/"}

    def test_multiple_targets_collect_all(self) -> None:
        t1 = _make_target(root_dir=".github", subdir="extensions")
        t2 = _make_target(name="claude")
        t2.primitives = {}
        t3 = _make_target(deploy_root=".copilot", subdir="extensions")
        prefixes = _canvas_deploy_prefixes([t1, t2, t3])
        assert ".github/extensions/" in prefixes
        assert ".copilot/extensions/" in prefixes


# ------------------------------------------------------------------
# is_canvas_bundle_path trust gate helper
# ------------------------------------------------------------------


class TestIsCanvasBundlePath:
    @pytest.mark.parametrize(
        "rel",
        [
            "extensions/my-canvas/extension.mjs",
            ".github/extensions/demo/extension.mjs",
            ".github/extensions/demo/lib/helper.mjs",
            ".copilot/extensions/x/extension.mjs",
        ],
    )
    def test_positive_cases(self, rel: str) -> None:
        assert is_canvas_bundle_path(rel) is True

    @pytest.mark.parametrize(
        "rel",
        [
            "skills/my-skill/SKILL.md",
            ".github/workflows/ci.yml",
            "agents/my-agent.md",
            "prompts/prompt.md",
            "some/deep/extensions/not-a-canvas/file.mjs",
        ],
    )
    def test_negative_cases(self, rel: str) -> None:
        assert is_canvas_bundle_path(rel) is False

    def test_case_insensitive_extensions_segment(self) -> None:
        assert is_canvas_bundle_path("Extensions/my-canvas/extension.mjs") is True
        assert is_canvas_bundle_path(".github/EXTENSIONS/demo/file.mjs") is True
        assert is_canvas_bundle_path(".github/ExTeNsIoNs/demo/file.mjs") is True

    def test_backslash_normalisation(self) -> None:
        assert is_canvas_bundle_path("extensions\\demo\\extension.mjs") is True
        assert is_canvas_bundle_path(".github\\extensions\\x\\f.mjs") is True


# ------------------------------------------------------------------
# Canvas sync (uninstall)
# ------------------------------------------------------------------


class TestCanvasSync:
    def test_sync_calls_remove_for_canvas_prefix(self, tmp_path: Path) -> None:
        """sync_for_target routes removal to the correct prefix."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        target = _make_target()
        managed = {
            ".github/extensions/old-canvas/extension.mjs",
            ".github/extensions/old-canvas/style.css",
        }

        integrator = CanvasIntegrator()

        with patch(
            "apm_cli.integration.canvas_integrator.BaseIntegrator.sync_remove_files",
            return_value={"files_removed": 2, "errors": 0},
        ) as mock_sync:
            result = integrator.sync_for_target(
                target,
                MagicMock(),
                project_root,
                managed_files=managed,
            )

        mock_sync.assert_called_once()
        call_args = mock_sync.call_args
        # Verify prefix
        assert call_args.kwargs["prefix"] == ".github/extensions/"
        assert result["files_removed"] == 2

    def test_sync_no_canvas_mapping_returns_zero(self, tmp_path: Path) -> None:
        """sync_for_target with no canvas mapping is a no-op."""
        target = MagicMock()
        target.name = "copilot"
        target.primitives = {}

        integrator = CanvasIntegrator()
        result = integrator.sync_for_target(
            target,
            MagicMock(),
            tmp_path,
            managed_files=set(),
        )

        assert result == {"files_removed": 0, "errors": 0}


# ------------------------------------------------------------------
# Name validation
# ------------------------------------------------------------------


class TestCanvasNameValidation:
    def test_valid_names_pass(self) -> None:
        integrator = CanvasIntegrator()
        for name in ["my-canvas", "demo_widget", "Canvas.v2", "a1"]:
            integrator._validate_canvas_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            ".hidden",
            "trailing.",
            "../traversal",
            "con",
            "nul",
            "a/b",
            "",
        ],
    )
    def test_invalid_names_raise(self, name: str) -> None:
        integrator = CanvasIntegrator()
        with pytest.raises((ValueError, Exception)):
            integrator._validate_canvas_name(name)
