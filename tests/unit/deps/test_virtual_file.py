"""Tests for ``materialize_virtual_file_package``."""

from __future__ import annotations

from pathlib import Path

from apm_cli.deps.virtual_file import materialize_virtual_file_package
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference


def test_rematerialization_invalidates_cached_manifest(tmp_path: Path) -> None:
    """#3585: rewriting the package-tree apm.yml must drop the cached
    from_apm_yml instance so a same-process reload sees the new bytes."""
    dep_ref = DependencyReference.parse("owner/repo/prompts/demo.prompt.md")
    target = tmp_path / "demo"

    materialize_virtual_file_package(
        dep_ref, target, "demo.prompt.md", b"---\ndescription: First\n---\n\n# Demo\n"
    )
    first = APMPackage.from_apm_yml(target / "apm.yml")
    assert first.description == "First"

    materialize_virtual_file_package(
        dep_ref, target, "demo.prompt.md", b"---\ndescription: Second\n---\n\n# Demo\n"
    )
    reloaded = APMPackage.from_apm_yml(target / "apm.yml")
    assert reloaded is not first
    assert reloaded.description == "Second"
