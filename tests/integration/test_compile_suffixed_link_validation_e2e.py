"""Installed-CLI coverage for compile-time validation of suffixed links (#3255).

A relative Markdown link may carry a ``#fragment``, ``?query`` or both. The
installed ``apm compile`` must validate the file path without the suffix: an
existing file is not reported missing, a missing file is reported by its path
alone, the emitted link keeps its suffix, and ``--dry-run`` writes nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-compiled-output-writes"),
]

_SUFFIXES = ("#template", "?view=full", "?view=full#template")


def _create_project(project: Path) -> None:
    (project / "apm.yml").write_text(
        "name: suffixed-links\nversion: 0.1.0\ntargets:\n  - codex\n", encoding="ascii"
    )
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    links = []
    for index, suffix in enumerate(_SUFFIXES):
        links.append(f"[present{index}](references/standards.md{suffix})")
        links.append(f"[absent{index}](references/missing.md{suffix})")
    (instructions / "links.instructions.md").write_text(
        "---\ndescription: Suffixed links\napplyTo: '**'\n---\n# Links\n" + "\n".join(links) + "\n",
        encoding="ascii",
    )
    references = instructions / "references"
    references.mkdir()
    (references / "standards.md").write_text("# Template\n", encoding="ascii")


def _compile(apm_binary_path: Path, project: Path, *extra: str) -> str:
    result = subprocess.run(
        (str(apm_binary_path), "compile", *extra),
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return " ".join((result.stdout + result.stderr).split())


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_installed_compile_validates_suffixed_link_paths(
    tmp_path: Path, apm_binary_path: Path, dry_run: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _create_project(project)

    output = _compile(apm_binary_path, project, *(("--dry-run",) if dry_run else ()))

    assert "Referenced file not found: references/standards.md" not in output
    for index in range(len(_SUFFIXES)):
        assert (
            f"Referenced file not found: references/missing.md (in link 'absent{index}')" in output
        )
    assert "missing.md#" not in output
    assert "missing.md?" not in output

    agents_md = project / "AGENTS.md"
    if dry_run:
        assert not agents_md.exists()
        return
    emitted = agents_md.read_text(encoding="utf-8")
    for suffix in _SUFFIXES:
        assert f"standards.md{suffix})" in emitted
