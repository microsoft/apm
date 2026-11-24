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
