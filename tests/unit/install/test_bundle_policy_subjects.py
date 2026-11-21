"""The package identities a local bundle presents to the org dependency deny list."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.bundle_admission import bundle_policy_subjects

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-violation-subjects")]


def _bundle(root: Path, lock: str | None) -> SimpleNamespace:
    root.mkdir()
    if lock is not None:
        (root / "apm.lock.yaml").write_text(lock, encoding="utf-8")
    return SimpleNamespace(
        source_dir=root, package_id="tool-id", plugin_json={"name": "tool"}, lockfile=None
    )


def test_bundle_presents_its_names_and_every_package_it_records(tmp_path: Path) -> None:
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="acme/hooky", resolved_commit="a" * 40))
    bundle = _bundle(tmp_path / "b", lock.to_yaml())

    keys = [ref.get_canonical_dependency_string() for ref in bundle_policy_subjects(bundle)]

    assert keys == ["_local/tool", "_local/tool-id", "acme/hooky"]


def test_unreadable_bundle_lockfile_leaves_the_bundle_names(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "b", "dependencies: [unclosed\n")

    keys = [ref.get_canonical_dependency_string() for ref in bundle_policy_subjects(bundle)]

    assert keys == ["_local/tool", "_local/tool-id"]
