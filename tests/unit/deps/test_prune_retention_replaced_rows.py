"""Prune keeps a replaced transitive row under the remote retention policy (#4989).

Only a transitive local dependency with no replacement becomes an orphan
candidate; a remote-owned row replaced with local bytes keeps the policy
remote transitives always had, so a retained parent the walk never visits
cannot strand it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.reachability import retained_lock_keys
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import (
    ReplacementBinding,
    parse_replace_block,
    project_effective_reference,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-orphan-package-selection")]


def test_a_remote_owned_row_replaced_with_local_bytes_is_retained(tmp_path: Path) -> None:
    block = [{"for": {"git": "https://github.com/acme/base"}, "with": {"path": "../base"}}]
    original = DependencyReference(repo_url="acme/base", host="github.com", reference="v1")
    rule = parse_replace_block(block)[0]
    binding = ReplacementBinding(original, rule)
    binding.record(original, "acme/r", False)
    effective = project_effective_reference(original, rule)
    effective.replacement = binding
    lockfile = LockFile()
    lockfile.replacements = block
    lockfile.add_dependency(
        LockedDependency(repo_url="acme/r", resolved_commit="r" * 40, depth=2, resolved_by="acme/d")
    )
    lockfile.add_dependency(
        LockedDependency.from_dependency_ref(
            dep_ref=effective, resolved_commit=None, depth=3, resolved_by="acme/r"
        )
    )
    base_key = next(key for key in lockfile.dependencies if key != "acme/r")

    retained = retained_lock_keys(lockfile, tmp_path, tmp_path / "apm_modules", [])

    assert base_key in retained
    assert "acme/r" in retained


def _remote_recorded_under_a_dropped_root(tmp_path: Path) -> tuple[LockFile, str]:
    """apm.yml declares ./vendor/b; dropped ./vendor/a and b both declare acme/r, which
    the lock records under a."""
    from apm_cli.deps.lockfile import lockfile_parent_identity

    for name in ("a", "b"):
        package = tmp_path / "vendor" / name
        package.mkdir(parents=True)
        (package / "apm.yml").write_text(
            f"name: {name}\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/r\n", "utf-8"
        )
    remote = tmp_path / "apm_modules" / "acme" / "r"
    remote.mkdir(parents=True)
    (remote / "apm.yml").write_text("name: r\nversion: 1.0.0\n", "utf-8")
    lockfile = LockFile()
    rows = {}
    for name in ("a", "b"):
        rows[name] = LockedDependency.from_dependency_ref(
            dep_ref=DependencyReference.parse(f"./vendor/{name}"),
            resolved_commit=None,
            depth=1,
            resolved_by=None,
        )
        lockfile.add_dependency(rows[name])
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/r",
            resolved_commit="r" * 40,
            depth=2,
            resolved_by=lockfile_parent_identity(rows["a"], lockfile.replacements),
        )
    )
    return lockfile, rows["a"].get_unique_key()


def test_retention_does_not_depend_on_the_walks_collector(tmp_path: Path) -> None:
    """#5449 review: a remote row the declared walk reaches is re-parented, so its dropped
    recorded parent goes whether or not the caller collects the walk."""
    lockfile, a_key = _remote_recorded_under_a_dropped_root(tmp_path)
    declared = [DependencyReference.parse("./vendor/b")]
    modules = tmp_path / "apm_modules"

    omitted = retained_lock_keys(lockfile, tmp_path, modules, declared)
    collected = retained_lock_keys(lockfile, tmp_path, modules, declared, walks=[])

    assert omitted == collected
    assert a_key not in collected
    assert "acme/r" in collected
