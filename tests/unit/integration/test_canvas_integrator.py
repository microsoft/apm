"""Unit tests for the experimental Copilot canvas integrator.

Covers the two-gate model (experimental flag + dependency trust gate),
Copilot-only / project-scope restrictions, atomic per-bundle deploy,
content adoption, name validation, sync/uninstall removal, and the
bundle-path detector shared with the offline install / unpack paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.integration.canvas_integrator import (
    CanvasIntegrator,
    is_canvas_bundle_path,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.diagnostics import DiagnosticCollector

# ---------------------------------------------------------------------------
# Config injection (mirrors tests/unit/test_external_scanners.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache():
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture
def enable_canvas(monkeypatch):
    """Enable the ``canvas`` experimental flag for the test body."""
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"canvas": True}})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_canvas(package_root: Path, name: str = "demo", *, marker: bool = True) -> Path:
    """Create ``<package_root>/.apm/extensions/<name>/`` with optional marker."""
    bundle = package_root / ".apm" / "extensions" / name
    bundle.mkdir(parents=True, exist_ok=True)
    if marker:
        (bundle / "extension.mjs").write_text(f"export default {{ name: {name!r} }};\n")
        (bundle / "helper.js").write_text("export const h = 1;\n")
    return bundle


def _pkg_info(install_path: Path):
    return SimpleNamespace(install_path=str(install_path))


def _copilot():
    return KNOWN_TARGETS["copilot"]


def _claude():
    return KNOWN_TARGETS["claude"]


def _deployed_rel(result, project_root: Path) -> set[str]:
    return {Path(p).relative_to(project_root).as_posix() for p in result.target_paths}


# ---------------------------------------------------------------------------
# is_canvas_bundle_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("extensions/demo/extension.mjs", True),
        (".github/extensions/demo/extension.mjs", True),
        (".copilot/extensions/widget/a.js", True),
        ("extensions/demo", True),
        ("skills/foo/extensions/bar.md", False),
        ("agents/a.md", False),
        ("extensionsfoo/x.js", False),
        ("commands/extensions.md", False),
    ],
)
def test_is_canvas_bundle_path(rel: str, expected: bool):
    assert is_canvas_bundle_path(rel) is expected


# ---------------------------------------------------------------------------
# find_canvas_bundles
# ---------------------------------------------------------------------------


def test_find_canvas_bundles_requires_marker(tmp_path: Path):
    _make_canvas(tmp_path, "withmarker")
    # A directory without the marker is ignored.
    (tmp_path / ".apm" / "extensions" / "nomarker").mkdir(parents=True)
    bundles = CanvasIntegrator.find_canvas_bundles(tmp_path)
    assert [b.name for b in bundles] == ["withmarker"]


def test_find_canvas_bundles_rejects_symlinked_dir(tmp_path: Path):
    real = _make_canvas(tmp_path, "real")
    link = tmp_path / ".apm" / "extensions" / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")
    bundles = CanvasIntegrator.find_canvas_bundles(tmp_path)
    assert [b.name for b in bundles] == ["real"]


def test_find_canvas_bundles_empty_when_absent(tmp_path: Path):
    assert CanvasIntegrator.find_canvas_bundles(tmp_path) == []


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["con", "PRN", "nul", "lpt1", ".hidden", "trailing.", "bad/name"])
def test_validate_canvas_name_rejects(bad: str):
    with pytest.raises((ValueError,)):
        CanvasIntegrator._validate_canvas_name(bad)


@pytest.mark.parametrize("ok", ["demo", "my-canvas", "a_b.c", "Widget1"])
def test_validate_canvas_name_accepts(ok: str):
    CanvasIntegrator._validate_canvas_name(ok)


# ---------------------------------------------------------------------------
# Flag gating
# ---------------------------------------------------------------------------


def test_flag_off_is_noop(tmp_path: Path):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg)
    project = tmp_path / "proj"
    project.mkdir()
    # No enable_canvas fixture -> flag defaults off.
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        is_first_party=True,
    )
    assert result.files_integrated == 0
    assert not (project / ".github" / "extensions").exists()


# ---------------------------------------------------------------------------
# First-party deploy
# ---------------------------------------------------------------------------


def test_first_party_deploys(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "demo")
    project = tmp_path / "proj"
    project.mkdir()
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        is_first_party=True,
    )
    assert result.files_integrated == 1
    rels = _deployed_rel(result, project)
    assert ".github/extensions/demo/extension.mjs" in rels
    assert ".github/extensions/demo/helper.js" in rels
    assert (project / ".github" / "extensions" / "demo" / "extension.mjs").is_file()


# ---------------------------------------------------------------------------
# Dependency trust gate
# ---------------------------------------------------------------------------


def test_dependency_blocked_without_trust(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "widget")
    project = tmp_path / "proj"
    project.mkdir()
    diags = DiagnosticCollector()
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        diagnostics=diags,
        trust_canvas=False,
        is_first_party=False,
        package_name="acme/widgets",
    )
    assert result.files_integrated == 0
    assert not (project / ".github" / "extensions").exists()
    messages = " ".join(d.message for d in diags._diagnostics)
    assert "widget" in messages
    assert "--trust-canvas-extensions" in messages


def test_dependency_deploys_with_trust(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "widget")
    project = tmp_path / "proj"
    project.mkdir()
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        trust_canvas=True,
        is_first_party=False,
        package_name="acme/widgets",
    )
    assert result.files_integrated == 1
    assert (project / ".github" / "extensions" / "widget" / "extension.mjs").is_file()


# ---------------------------------------------------------------------------
# Target / scope restrictions
# ---------------------------------------------------------------------------


def test_non_copilot_target_is_noop(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg)
    project = tmp_path / "proj"
    project.mkdir()
    result = CanvasIntegrator().integrate_canvases_for_target(
        _claude(),
        _pkg_info(pkg),
        project,
        is_first_party=True,
    )
    assert result.files_integrated == 0
    assert not (project / ".claude" / "extensions").exists()


def test_user_scope_is_noop(tmp_path: Path, enable_canvas):
    from apm_cli.core.scope import InstallScope

    pkg = tmp_path / "pkg"
    _make_canvas(pkg)
    project = tmp_path / "proj"
    project.mkdir()
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        scope=InstallScope.USER,
        is_first_party=True,
    )
    assert result.files_integrated == 0


# ---------------------------------------------------------------------------
# Atomic collision + adoption
# ---------------------------------------------------------------------------


def test_unmanaged_collision_skips_whole_bundle(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "demo")
    project = tmp_path / "proj"
    dest_dir = project / ".github" / "extensions" / "demo"
    dest_dir.mkdir(parents=True)
    # Pre-existing, unmanaged, divergent file at one bundle path.
    (dest_dir / "helper.js").write_text("local edit\n")
    diags = DiagnosticCollector()
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        diagnostics=diags,
        is_first_party=True,
    )
    assert result.files_integrated == 0
    assert result.files_skipped == 1
    # The whole bundle is skipped: the marker is never written.
    assert not (dest_dir / "extension.mjs").exists()
    # The local edit is preserved.
    assert (dest_dir / "helper.js").read_text() == "local edit\n"


def test_force_overwrites_collision(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "demo")
    project = tmp_path / "proj"
    dest_dir = project / ".github" / "extensions" / "demo"
    dest_dir.mkdir(parents=True)
    (dest_dir / "helper.js").write_text("local edit\n")
    result = CanvasIntegrator().integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        force=True,
        is_first_party=True,
    )
    assert result.files_integrated == 1
    assert (dest_dir / "extension.mjs").is_file()
    assert (dest_dir / "helper.js").read_text() == "export const h = 1;\n"


def test_byte_identical_reinstall_is_adopted(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "demo")
    project = tmp_path / "proj"
    project.mkdir()
    integrator = CanvasIntegrator()
    first = integrator.integrate_canvases_for_target(
        _copilot(), _pkg_info(pkg), project, is_first_party=True
    )
    assert first.files_integrated == 1
    managed = {Path(p).relative_to(project).as_posix() for p in first.target_paths}
    second = integrator.integrate_canvases_for_target(
        _copilot(),
        _pkg_info(pkg),
        project,
        managed_files=managed,
        is_first_party=True,
    )
    assert second.files_integrated == 0
    assert second.files_adopted == 1


# ---------------------------------------------------------------------------
# Sync / uninstall
# ---------------------------------------------------------------------------


def test_sync_removes_canvas_and_empty_dirs(tmp_path: Path, enable_canvas):
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "demo")
    project = tmp_path / "proj"
    project.mkdir()
    integrator = CanvasIntegrator()
    result = integrator.integrate_canvases_for_target(
        _copilot(), _pkg_info(pkg), project, is_first_party=True
    )
    managed = {Path(p).relative_to(project).as_posix() for p in result.target_paths}
    assert (project / ".github" / "extensions" / "demo" / "extension.mjs").is_file()

    stats = integrator.sync_for_target(
        _copilot(), SimpleNamespace(), project, managed_files=managed
    )
    assert stats["files_removed"] >= 2
    assert not (project / ".github" / "extensions" / "demo").exists()


def test_sync_not_gated_by_flag(tmp_path: Path):
    """Uninstall must work even when the experimental flag is off."""
    pkg = tmp_path / "pkg"
    _make_canvas(pkg, "demo")
    project = tmp_path / "proj"
    dest = project / ".github" / "extensions" / "demo"
    dest.mkdir(parents=True)
    (dest / "extension.mjs").write_text("export default {};\n")
    managed = {".github/extensions/demo/extension.mjs"}
    stats = CanvasIntegrator().sync_for_target(
        _copilot(), SimpleNamespace(), project, managed_files=managed
    )
    assert stats["files_removed"] == 1
    assert not dest.exists()


# ---------------------------------------------------------------------------
# Two-canvas uninstall survival (only the removed package's canvas goes)
# ---------------------------------------------------------------------------


def test_sync_only_removes_managed_subset(tmp_path: Path, enable_canvas):
    project = tmp_path / "proj"
    # Two canvases deployed from two different packages.
    for name in ("alpha", "beta"):
        pkg = tmp_path / f"pkg_{name}"
        _make_canvas(pkg, name)
        CanvasIntegrator().integrate_canvases_for_target(
            _copilot(), _pkg_info(pkg), project, is_first_party=True
        )
    assert (project / ".github" / "extensions" / "alpha" / "extension.mjs").is_file()
    assert (project / ".github" / "extensions" / "beta" / "extension.mjs").is_file()

    # Uninstall only alpha.
    alpha_managed = {
        os.path.relpath(str(p), str(project)).replace(os.sep, "/")
        for p in (project / ".github" / "extensions" / "alpha").rglob("*")
        if p.is_file()
    }
    CanvasIntegrator().sync_for_target(
        _copilot(), SimpleNamespace(), project, managed_files=alpha_managed
    )
    assert not (project / ".github" / "extensions" / "alpha").exists()
    # beta survives.
    assert (project / ".github" / "extensions" / "beta" / "extension.mjs").is_file()


# ---------------------------------------------------------------------------
# Dispatch first-party signal (regression for the package-name spoof)
# ---------------------------------------------------------------------------


def _canvas_only_bundle():
    """An IntegratorBundle wiring canvas + a real (no-op) skill integrator.

    ``integrate_package_primitives`` always invokes the skill integrator
    outside the dispatch loop, so a real ``SkillIntegrator`` is supplied
    (the test package carries no skills, so it is a no-op).  The remaining
    integrators are ``None`` -- the dispatch loop skips them.
    """
    from apm_cli.install.services import IntegratorBundle
    from apm_cli.integration.skill_integrator import SkillIntegrator

    return IntegratorBundle(
        prompt=None,
        agent=None,
        skill=SkillIntegrator(),
        instruction=None,
        command=None,
        hook=None,
        canvas=CanvasIntegrator(),
    )


def _dispatch_pkg_info(install_path: Path):
    """A package_info rich enough for the full dispatch (skill branch inert)."""
    from apm_cli.models.apm_package import PackageType

    return SimpleNamespace(
        install_path=Path(install_path),
        package_type=PackageType.APM_PACKAGE,
        package=SimpleNamespace(name="dep"),
        dependency_ref=None,
    )


def test_dispatch_dependency_named_local_is_not_first_party(tmp_path: Path, enable_canvas):
    """A dependency literally named '_local' must still hit the trust gate.

    First-party status is decided by the call path (the ``is_first_party``
    kwarg), never inferred from the package name, so an attacker cannot
    bypass the trust gate by naming their package ``_local``.
    """
    from apm_cli.install.services import integrate_package_primitives

    _make_canvas(tmp_path, "widget")
    project_root = tmp_path / "proj"
    project_root.mkdir()
    diags = DiagnosticCollector()

    result = integrate_package_primitives(
        _dispatch_pkg_info(tmp_path),
        project_root,
        targets=[_copilot()],
        integrators=_canvas_only_bundle(),
        force=False,
        managed_files=set(),
        diagnostics=diags,
        package_name="_local",
        ctx=SimpleNamespace(trust_canvas=False, verbose=False),
        # is_first_party defaults to False (dependency call path)
    )

    assert result["canvases"] == 0
    assert not (project_root / ".github" / "extensions" / "widget").exists()


def test_dispatch_first_party_flag_deploys(tmp_path: Path, enable_canvas):
    """The local-content call path passes is_first_party=True and deploys."""
    from apm_cli.install.services import integrate_package_primitives

    _make_canvas(tmp_path, "widget")
    project_root = tmp_path / "proj"
    project_root.mkdir()
    diags = DiagnosticCollector()

    result = integrate_package_primitives(
        _dispatch_pkg_info(tmp_path),
        project_root,
        targets=[_copilot()],
        integrators=_canvas_only_bundle(),
        force=False,
        managed_files=set(),
        diagnostics=diags,
        package_name="owner/dep",
        ctx=SimpleNamespace(trust_canvas=False, verbose=False),
        is_first_party=True,
    )

    assert result["canvases"] == 1
    assert (project_root / ".github" / "extensions" / "widget" / "extension.mjs").is_file()


def test_dispatch_dependency_deploys_with_trust(tmp_path: Path, enable_canvas):
    """A dependency canvas deploys when the operator trusts canvas extensions."""
    from apm_cli.install.services import integrate_package_primitives

    _make_canvas(tmp_path, "widget")
    project_root = tmp_path / "proj"
    project_root.mkdir()
    diags = DiagnosticCollector()

    result = integrate_package_primitives(
        _dispatch_pkg_info(tmp_path),
        project_root,
        targets=[_copilot()],
        integrators=_canvas_only_bundle(),
        force=False,
        managed_files=set(),
        diagnostics=diags,
        package_name="owner/dep",
        ctx=SimpleNamespace(trust_canvas=True, verbose=False),
    )

    assert result["canvases"] == 1
    assert (project_root / ".github" / "extensions" / "widget" / "extension.mjs").is_file()
