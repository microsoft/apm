"""End-to-end coverage for ``apm compile --root DIR``.

The headline promise of the ``--root`` flag (issue #888) is a clean
split: sources resolve from ``$PWD`` while every generated artifact is
written under ``DIR``. These tests pin that contract end-to-end through
a real subprocess (no in-process Click harness) so a regression in the
chdir + source-root-override mechanism surfaces as a behavioral diff,
not just a unit-level assertion.

Two invariants are load-bearing:

* **byte-identical** -- the artifact written under ``--root`` is
  identical to the one a plain ``apm compile`` would have produced in
  ``$PWD``; ``--root`` only moves *where* output lands, never *what* is
  generated.
* **no source-tree pollution + no leakage** -- the source tree stays
  clean, and a subsequent plain ``apm compile`` (no ``--root``) once
  again writes into ``$PWD``, proving the process-global override did
  not leak across invocations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _make_project(root: Path) -> None:
    (root / "apm.yml").write_text("name: root-redirect-e2e\nversion: 0.1.0\n", encoding="utf-8")
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "demo.instructions.md").write_text(
        '---\ndescription: Demo guide\napplyTo: "**"\n---\n\n'
        "# Demo\n\nHello from the source tree.\n",
        encoding="utf-8",
    )


def _run(
    apm_binary_path: Path,
    args: list[str],
    cwd: Path,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(apm_binary_path), "compile", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_root_redirect_is_byte_identical_and_keeps_sources_in_pwd(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _make_project(src)

    # Baseline: plain compile writes AGENTS.md into the source tree.
    baseline = _run(apm_binary_path, [], cwd=src)
    assert baseline.returncode == 0, baseline.stderr or baseline.stdout
    baseline_agents = src / "AGENTS.md"
    assert baseline_agents.exists(), "baseline compile did not emit AGENTS.md"
    baseline_bytes = baseline_agents.read_bytes()
    baseline_agents.unlink()

    # --root: sources still read from ``src`` ($PWD), writes land in deploy.
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    redirected = _run(apm_binary_path, ["--root", str(deploy)], cwd=src)
    assert redirected.returncode == 0, redirected.stderr or redirected.stdout

    # Source tree stays clean -- no artifact leaked back into $PWD.
    assert not (src / "AGENTS.md").exists(), "--root leaked AGENTS.md into $PWD"

    # Output landed under the deploy root and is byte-identical.
    deployed_agents = deploy / "AGENTS.md"
    assert deployed_agents.exists(), "--root did not emit AGENTS.md under DIR"
    assert deployed_agents.read_bytes() == baseline_bytes, (
        "--root output diverged from baseline; the flag must only change "
        "WHERE output lands, never WHAT is generated"
    )


def test_root_override_does_not_leak_into_next_invocation(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _make_project(src)

    deploy = tmp_path / "deploy"
    deploy.mkdir()

    first = _run(apm_binary_path, ["--root", str(deploy)], cwd=src)
    assert first.returncode == 0, first.stderr or first.stdout
    assert (deploy / "AGENTS.md").exists()
    assert not (src / "AGENTS.md").exists()

    # A subsequent plain compile (separate process, but the override is a
    # process-global so this also guards the in-process unwind contract)
    # must once again write into $PWD, not the previous deploy root.
    second = _run(apm_binary_path, [], cwd=src)
    assert second.returncode == 0, second.stderr or second.stdout
    assert (src / "AGENTS.md").exists(), (
        "plain compile after --root failed to write into $PWD -- "
        "the source-root override leaked across invocations"
    )
