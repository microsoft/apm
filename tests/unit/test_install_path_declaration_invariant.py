"""Invariant: ``get_install_path`` and ``get_dependency_declaration_order``
must produce paths that agree, for every flavor of dependency reference.

Why this test exists
--------------------
``DependencyReference.get_install_path()`` and
``primitives.discovery.get_dependency_declaration_order()`` are two
independently-written code paths that both compute "where does this
package live in ``apm_modules/``?" -- one returns an absolute ``Path``,
the other returns a relative POSIX-style string used by orphan-detection
and primitive discovery. If they disagree, the orphan detector flags
correctly-installed packages as orphans and the primitive scanner misses
real packages.

The cluster-6 failure in the merge-queue run for #1238 was exactly this
class of drift: PR #1094 changed ``collections/<name>`` from the deleted
``COLLECTION`` virtual type to ``SUBDIRECTORY``, which made
``get_install_path()`` and ``get_dependency_declaration_order()`` start
using slash-form paths; tests caught it because they hard-coded the old
flattened name -- but no invariant test asserted the two functions agree.

Pinning the agreement directly catches future drift in either function
(or in ``is_virtual_subdirectory()`` / ``get_virtual_package_name()``)
in <10ms instead of waiting for a slow E2E to misbehave.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.primitives.discovery import get_dependency_declaration_order


def _project_with_deps(tmp_path: Path, deps: list[str]) -> Path:
    """Write a minimal ``apm.yml`` declaring *deps* and return its dir."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "harness",
                "version": "1.0.0",
                "dependencies": {"apm": deps},
            }
        )
    )
    return proj


def _install_path_str(dep_str: str, apm_modules: Path) -> str:
    """Compute install path for *dep_str* and return it relative to apm_modules."""
    ref = DependencyReference.parse(dep_str)
    abs_path = ref.get_install_path(apm_modules)
    rel = abs_path.relative_to(apm_modules)
    return rel.as_posix()


# ---------------------------------------------------------------------------
# The invariant matrix: one case per dependency flavor.
# ---------------------------------------------------------------------------

# Each row: (dep_string, expected_relative_path).
# Expected paths are the post-#1094 contract: virtual *subdirectory* packages
# (Claude Skills, ADO ``collections/<name>``) keep their natural slash path;
# virtual *file* packages flatten via ``get_virtual_package_name()``.
INVARIANT_MATRIX: list[tuple[str, str]] = [
    # Regular GitHub package
    ("acme/widgets", "acme/widgets"),
    # Regular ADO package -- 3-level org/project/repo
    (
        "dev.azure.com/contoso/platform/widgets",
        "contoso/platform/widgets",
    ),
    # Virtual subdirectory on GitHub (Claude Skills pattern)
    (
        "github/awesome-copilot/skills/review-and-refactor",
        "github/awesome-copilot/skills/review-and-refactor",
    ),
    # Virtual subdirectory on ADO -- post-#1094 collections layout
    (
        "dev.azure.com/contoso/platform/instructions/collections/csharp-ddd",
        "contoso/platform/instructions/collections/csharp-ddd",
    ),
]


@pytest.mark.parametrize("dep_str,expected_rel", INVARIANT_MATRIX)
def test_install_path_matches_declaration_order(tmp_path: Path, dep_str: str, expected_rel: str):
    """``get_install_path`` and ``get_dependency_declaration_order`` agree.

    For every supported reference flavor, the relative path produced by
    ``DependencyReference.get_install_path()`` must equal the path emitted
    by ``get_dependency_declaration_order()`` for the same ``apm.yml``.
    """
    proj = _project_with_deps(tmp_path, [dep_str])
    apm_modules = proj / "apm_modules"

    install_rel = _install_path_str(dep_str, apm_modules)
    declared = get_dependency_declaration_order(str(proj))

    assert install_rel == expected_rel, (
        f"get_install_path produced {install_rel!r}, expected {expected_rel!r}"
    )
    assert declared == [expected_rel], (
        "get_dependency_declaration_order disagrees with get_install_path: "
        f"declaration={declared!r} vs install={install_rel!r}"
    )


def test_ado_virtual_collection_uses_subdirectory_layout(tmp_path: Path):
    """Regression trap for cluster-6: ADO ``collections/<name>`` MUST be a
    virtual *subdirectory*, not a flattened virtual package.

    Pre-#1094, ADO ``collections/<name>`` was a dedicated COLLECTION virtual
    type that flattened to ``org/project/repo-<name>``. PR #1094 deleted that
    type so ``collections/<name>`` is now indistinguishable from any other
    virtual subdirectory and must keep its natural slash path everywhere.
    """
    dep_str = "dev.azure.com/contoso/platform/instructions/collections/csharp-ddd"
    ref = DependencyReference.parse(dep_str)

    assert ref.is_virtual, "ADO collections/<name> must be a virtual package"
    assert ref.is_virtual_subdirectory(), (
        "ADO collections/<name> must be classified as a virtual SUBDIRECTORY "
        "post-#1094 (the COLLECTION virtual type was deleted)"
    )

    rel = _install_path_str(dep_str, tmp_path / "apm_modules")
    # Must NOT be the flattened legacy form ``contoso/platform/instructions-csharp-ddd``.
    assert "instructions-csharp-ddd" not in rel, (
        "Install path regressed to the deleted COLLECTION flattened form"
    )
    assert rel == "contoso/platform/instructions/collections/csharp-ddd"


def test_declaration_order_preserves_apm_yml_order(tmp_path: Path):
    """Declaration order must be stable across mixed-flavor manifests.

    Catches regressions where dependency reordering (e.g. virtual deps moved
    after regular deps for resolver convenience) breaks downstream consumers
    that rely on apm.yml ordering for primitive precedence.
    """
    deps = [dep for dep, _ in INVARIANT_MATRIX]
    expected = [exp for _, exp in INVARIANT_MATRIX]

    proj = _project_with_deps(tmp_path, deps)
    declared = get_dependency_declaration_order(str(proj))

    assert declared == expected, (
        "get_dependency_declaration_order changed the order of apm.yml deps"
    )
