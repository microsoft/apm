"""Regression coverage for the shared/diamond transitive-dependency uninstall defect.

Two local packages (``root-a``, ``root-b``) both declare a local
transitive dependency on a third package (``shared``). Before the
forward-reachability fix (see :mod:`apm_cli.deps.reachability`),
uninstalling either parent incorrectly deleted ``shared`` from the
lockfile and ``apm_modules/`` even though the other parent still
declared and needed it -- ``LockedDependency.resolved_by`` is
single-valued/first-wins, and the backward orphan-candidate scan
(``_build_children_index``) only ever knew about the FIRST parent that
happened to install first.

``test_uninstall_last_shared_parent_removes_shared_dependency`` is the
negative twin and MUST perform two SEQUENTIAL uninstalls against the SAME
project state (not two independent single-shot scenarios): only that
shape exercises the ``resolved_by``/``local_path`` repair-on-rescue
mechanism (see ``engine._cleanup_transitive_orphans``) that keeps the
SECOND uninstall's backward-BFS candidate scan able to find ``shared`` as
a candidate at all, once its original first-wins parent is long gone.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache


def _write_local_package(root: Path, name: str, deps: list[str] | None = None) -> None:
    """Create a minimal local APM package, optionally declaring local *deps*."""
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", "version: 1.0.0"]
    if deps:
        lines.append("dependencies:")
        lines.append("  apm:")
        for dep_path in deps:
            lines.append(f"    - path: {dep_path}")
    (root / "apm.yml").write_text("\n".join(lines) + "\n", encoding="ascii")


def _write_shared_package(root: Path) -> None:
    """Create the ``shared`` leaf package with one deployable instruction file."""
    _write_local_package(root, "shared")
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "shared.instructions.md").write_text(
        "---\ndescription: shared diamond regression\napplyTo: '**'\n---\n# Shared\n",
        encoding="ascii",
    )


def _write_diamond_project(tmp_path: Path) -> Path:
    """Build root-a, root-b (both -> shared) and an app project depending on both.

    Both diamond parents are declared in the app's apm.yml from project
    inception and installed with a SINGLE ``apm install`` call -- a second,
    separate ``apm install`` call for an already-installed project is a
    known, unrelated no-op quirk that would otherwise mask this scenario.
    """
    _write_local_package(tmp_path / "root-a", "root-a", deps=["../shared"])
    _write_local_package(tmp_path / "root-b", "root-b", deps=["../shared"])
    _write_shared_package(tmp_path / "shared")

    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm:\n"
        "    - path: ../root-a\n"
        "    - path: ../root-b\n",
        encoding="ascii",
    )
    return project


def _lockfile_data(project: Path) -> dict:
    return yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="ascii"))


def _dep(lock_data: dict, repo_url: str) -> dict | None:
    for dep in lock_data.get("dependencies", []):
        if dep["repo_url"] == repo_url:
            return dep
    return None


def _normalized(output: str) -> str:
    """Collapse whitespace/newlines so line-wrapped rich output can be substring-matched."""
    return " ".join(output.split())


def _shared_manifests_on_disk(project: Path) -> list[Path]:
    """Every ``apm.yml`` under apm_modules/ whose parent dir is named 'shared'.

    Local transitive deps are namespaced under a declaring-parent-derived
    hash directory (e.g. ``apm_modules/_local/<hash>/shared/``), not a
    flat ``apm_modules/shared/`` -- this searches broadly instead of
    hardcoding that internal layout.
    """
    return list((project / "apm_modules").glob("**/shared/apm.yml"))


def test_uninstall_one_shared_parent_keeps_shared_dependency_alive(
    tmp_path: Path, monkeypatch
) -> None:
    """Removing one diamond parent must not delete a still-needed shared dep."""
    project = _write_diamond_project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output

    uninstall = runner.invoke(cli, ["uninstall", "../root-a"])
    assert uninstall.exit_code == 0, uninstall.output

    lock_data = _lockfile_data(project)
    assert _dep(lock_data, "_local/root-a") is None
    assert _dep(lock_data, "_local/root-b") is not None
    shared = _dep(lock_data, "_local/shared")
    assert shared is not None, "shared must survive: root-b still declares it"
    assert _shared_manifests_on_disk(project), "shared's on-disk source must survive too"
    # Note: does NOT assert survival of shared's deployed instructions file
    # here. A separate, pre-existing defect (confirmed independently on
    # unmodified main, unrelated to resolved_by/reachability) causes
    # _sync_integrations_after_uninstall's legacy-glob fallback to wipe ALL
    # integrated instructions files whenever the removed package's own
    # deployed_files set is empty -- Phase 2 then only re-integrates DIRECT
    # dependencies, permanently losing a surviving TRANSITIVE package's
    # instructions. Out of scope for this isolated fix; tracked separately.


def test_uninstall_last_shared_parent_removes_shared_dependency(
    tmp_path: Path, monkeypatch
) -> None:
    """Negative twin: once the LAST parent is also removed, shared IS removed.

    This must be one scenario with two sequential uninstalls, not two
    independent single-parent scenarios -- see module docstring.
    """
    project = _write_diamond_project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output

    first = runner.invoke(cli, ["uninstall", "../root-a"])
    assert first.exit_code == 0, first.output

    # The rescue must have repaired resolved_by to a currently-valid
    # parent (root-b) -- not left it pointing at the now-gone root-a.
    # Without this repair the SECOND uninstall below cannot find shared
    # as a backward-BFS candidate at all (see module docstring).
    mid_lock_data = _lockfile_data(project)
    shared_after_first = _dep(mid_lock_data, "_local/shared")
    assert shared_after_first is not None
    assert shared_after_first.get("resolved_by") == "_local/root-b"

    second = runner.invoke(cli, ["uninstall", "../root-b"])
    assert second.exit_code == 0, second.output

    lockfile_path = project / "apm.lock.yaml"
    if lockfile_path.exists():
        lock_data = _lockfile_data(project)
        assert _dep(lock_data, "_local/shared") is None, (
            "shared must be garbage-collected once its last real parent is removed"
        )
        assert _dep(lock_data, "_local/root-b") is None
    # An empty lockfile is unlinked entirely (lockfile_has_persisted_state),
    # which is an equally valid proof that nothing (including shared) survives.
    assert not _shared_manifests_on_disk(project)


def test_uninstall_preserves_candidates_when_survivor_manifest_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Fail-closed: a missing survivor manifest preserves candidate orphans."""
    project = _write_diamond_project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output

    # Corrupt root-b's real on-disk manifest out from under it (simulates a
    # removed/corrupted source directory) so its reachability cannot be
    # verified during the walk triggered by uninstalling root-a.
    (tmp_path / "root-b" / "apm.yml").unlink()
    # The install above already cached root-b's (now-deleted) manifest;
    # without clearing, the reachability walk would read the stale cached
    # APMPackage instead of observing the real, now-missing file.
    clear_apm_yml_cache()

    uninstall = runner.invoke(cli, ["uninstall", "../root-a"])
    assert uninstall.exit_code == 0, uninstall.output
    assert "could not be verified" in _normalized(uninstall.output)

    lock_data = _lockfile_data(project)
    assert _dep(lock_data, "_local/shared") is not None, (
        "shared must be preserved when reachability cannot be verified"
    )


def test_uninstall_preserves_candidates_when_survivor_manifest_malformed(
    tmp_path: Path, monkeypatch
) -> None:
    """Fail-closed: a malformed survivor manifest preserves candidate orphans."""
    project = _write_diamond_project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output

    (tmp_path / "root-b" / "apm.yml").write_text(
        "name: [this is not valid: yaml: content\n", encoding="ascii"
    )
    clear_apm_yml_cache()

    uninstall = runner.invoke(cli, ["uninstall", "../root-a"])
    assert uninstall.exit_code == 0, uninstall.output
    assert "could not be verified" in _normalized(uninstall.output)

    lock_data = _lockfile_data(project)
    assert _dep(lock_data, "_local/shared") is not None, (
        "shared must be preserved when reachability cannot be verified"
    )


def test_uninstall_dry_run_reports_incomplete_reachability(tmp_path: Path, monkeypatch) -> None:
    """Dry-run parity: an unverifiable survivor previews as PRESERVED, with a warning."""
    project = _write_diamond_project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output

    (tmp_path / "root-b" / "apm.yml").write_text(
        "name: [this is not valid: yaml: content\n", encoding="ascii"
    )
    clear_apm_yml_cache()

    dry_run = runner.invoke(cli, ["uninstall", "../root-a", "--dry-run"])
    assert dry_run.exit_code == 0, dry_run.output
    assert "could not be verified" in _normalized(dry_run.output)
    assert "_local/shared" not in dry_run.output

    # Preview must not mutate anything on disk.
    lock_data = _lockfile_data(project)
    assert _dep(lock_data, "_local/root-a") is not None
    assert _dep(lock_data, "_local/shared") is not None
