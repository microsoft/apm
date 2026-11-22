"""OpenAPM req-mf-016: one local path spelled two ways is one local-path entry (#4731)."""

from __future__ import annotations

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = [pytest.mark.unit, pytest.mark.promise("req-mf-016")]


@pytest.mark.req("req-mf-016")
def test_consumer_recognises_two_relative_spellings_of_one_local_path(tmp_path):
    """#4731: ``./packages/lib`` (root) and ``../lib`` (sibling) are one local-path entry."""
    root_entry = DependencyReference.parse("./packages/lib")
    sibling_entry = DependencyReference.parse("../lib")
    assert root_entry.is_local and sibling_entry.is_local

    edge = APMDependencyResolver.transitive_local_identity(
        sibling_entry,
        declaring_parent="_local/sib",
        anchored=(tmp_path / "packages" / "sib" / "../lib").resolve(),
        root=tmp_path,
        root_local_dirs=APMDependencyResolver.root_local_dirs([root_entry], tmp_path),
    )

    assert edge.get_unique_key() == root_entry.get_unique_key()
