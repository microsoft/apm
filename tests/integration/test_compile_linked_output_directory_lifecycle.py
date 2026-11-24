"""Compile keeps compiled output inside the project when a deploy directory is a link.

``apm compile`` writes ``.github/copilot-instructions.md`` for the Copilot
target. When ``.github`` is a symlink to a directory outside the project,
compile refuses, exits non-zero and leaves the link target byte-identical,
matching the install-time refusal for linked deploy directories.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._containment_kit import (
    build_containment_kit,
    output,
    snapshot,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
    pytest.mark.e2e,
    pytest.mark.promise("P-OWN-compiled-output-writes"),
]

_GLOBAL_RULE = "---\ndescription: Global rule\n---\nGlobal body.\n"
_DOCS_RULE = '---\napplyTo: "docs/**"\ndescription: Docs rule\n---\nDocs body.\n'


def _write_compile_project(project: Path) -> None:
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 0.0.1\ntargets: [copilot]\n", encoding="utf-8"
    )
    rule = project / ".apm/instructions/global.instructions.md"
    rule.parent.mkdir(parents=True)
    rule.write_text(_GLOBAL_RULE, encoding="utf-8")


@pytest.mark.parametrize("link_kind", ["absolute", "relative", "dangling"])
def test_compile_refuses_linked_github_directory(
    tmp_path: Path, apm_binary_path: Path, link_kind: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    _write_compile_project(kit.project)
    (kit.outside / "existing.md").write_text("outside content\n", encoding="utf-8")
    link = kit.project / ".github"
    link_target = {
        "absolute": str(kit.outside),
        "relative": "../outside",
        "dangling": str(kit.outside / "missing"),
    }[link_kind]
    os.symlink(link_target, link, target_is_directory=True)
    before = snapshot(kit.outside)

    result = kit.run("compile")

    assert result.returncode != 0, output(result)
    assert "Refusing deployment through a symlinked path component" in output(result)
    assert ".github/copilot-instructions.md" in output(result)
    assert snapshot(kit.outside) == before
    assert not (kit.outside / "missing").exists()
    assert link.is_symlink()


@pytest.mark.parametrize("link_scope", ["outside", "inside"])
def test_compile_refuses_distributed_output_through_linked_directory(
    tmp_path: Path, apm_binary_path: Path, link_scope: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    (kit.project / "apm.yml").write_text(
        "name: consumer\nversion: 0.0.1\ntargets: [copilot]\n", encoding="utf-8"
    )
    rule = kit.project / ".apm/instructions/docs.instructions.md"
    rule.parent.mkdir(parents=True)
    rule.write_text(_DOCS_RULE, encoding="utf-8")
    linked_dir = kit.outside if link_scope == "outside" else kit.project / "real-docs"
    linked_dir.mkdir(exist_ok=True)
    (linked_dir / "guide.md").write_text("# Guide\n", encoding="utf-8")
    os.symlink(linked_dir, kit.project / "docs", target_is_directory=True)
    before = snapshot(linked_dir)

    result = kit.run("compile")

    assert result.returncode != 0, output(result)
    assert snapshot(linked_dir) == before
    assert not (linked_dir / "AGENTS.md").exists()
    if link_scope == "inside":
        assert "Refusing deployment through a symlinked path component: docs/AGENTS.md" in (
            output(result)
        )


def test_compile_writes_into_regular_github_directory(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    _write_compile_project(kit.project)

    result = kit.run("compile")

    assert result.returncode == 0, output(result)
    written = kit.project / ".github/copilot-instructions.md"
    assert "Global body." in written.read_text(encoding="utf-8")
    assert snapshot(kit.outside) == {}
