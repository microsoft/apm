"""Component contract: a safety rejection after uninstall consent is a failure (#4815).

`apm uninstall --trust-lockfile` keeps, without failing, a file its evidence
does not authorize deleting (#4295). That exemption covers evidence-based
retention only. When the kept file's parent is swapped for a symlink after
the preflight, cleanup's path-safety gate refuses it: the uninstall must
report the rejection as a failure, delete nothing outside the root and keep
the lockfile tracking the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup"),
    pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges"),
]

_PACKAGE = "owner/pkg"
_SKILL = ".agents/skills/alpha"
_KEPT = f"{_SKILL}/SKILL.md"


def _installed_with_an_edited_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One skill whose file was edited after install; no receipt, no render."""
    kept = tmp_path / _KEPT
    kept.parent.mkdir(parents=True)
    kept.write_text("deployed\n", encoding="utf-8")
    deployed_hash = compute_file_hash(kept)
    kept.write_text("deployed\nmy edit\n", encoding="utf-8")
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [_PACKAGE]},
        },
        tmp_path / "apm.yml",
    )
    dependency = LockedDependency(
        repo_url=_PACKAGE,
        deployed_files=[_SKILL, _KEPT],
        deployed_file_hashes={_KEPT: deployed_hash},
    )
    LockFile(dependencies={dependency.get_unique_key(): dependency}).write(
        tmp_path / "apm.lock.yaml"
    )
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
    monkeypatch.setattr("apm_cli.integration.cleanup._render_locked", lambda *_a, **_k: None)
    return kept


def _swap_parent_for_symlink_before_cleanup(
    monkeypatch: pytest.MonkeyPatch, skill_dir: Path, outside: Path
) -> None:
    """Replace *skill_dir* with a link to *outside* after preflight, before cleanup."""
    from importlib import import_module

    reconcile = import_module("apm_cli.install.manifest_reconcile")
    original = reconcile.reconcile_target_deployed_files

    def swapped(*args, **kwargs):
        skill_dir.rename(outside)
        skill_dir.symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(reconcile, "reconcile_target_deployed_files", swapped)


def test_safety_rejection_after_consent_fails_and_keeps_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = _installed_with_an_edited_skill(tmp_path, monkeypatch)
    outside = tmp_path / "outside-alpha"
    _swap_parent_for_symlink_before_cleanup(monkeypatch, kept.parent, outside)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(uninstall, [_PACKAGE, "--trust-lockfile"])

    assert result.exit_code == 1, result.output
    assert "Uninstall complete" not in result.output
    assert f"  - {_KEPT}" in result.output, result.output
    assert (outside / "SKILL.md").read_text(encoding="utf-8") == "deployed\nmy edit\n"
    assert (tmp_path / _SKILL).is_symlink()
    assert _PACKAGE in str(load_yaml(tmp_path / "apm.yml")["dependencies"]["apm"])
    lock = LockFile.read(tmp_path / "apm.lock.yaml")
    assert lock is not None
    tracked = {path for dep in lock.dependencies.values() for path in dep.deployed_files}
    assert _KEPT in tracked, tracked
