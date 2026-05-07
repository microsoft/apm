"""Performance smoke test for the drift replay engine.

The matrix calls out a 30-second budget for a small project end-to-end.
This unit-level test exercises ``run_replay`` directly with 100
synthetic ``.apm/instructions/*.instructions.md`` primitives and
asserts the replay completes in under 5 seconds on a stock dev box.

If this regresses you have likely:
  * Introduced an O(n^2) lookup in the diff engine, or
  * Re-enabled a network or disk-cache fallback inside the replay.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.drift import (
    CheckLogger,
    ReplayConfig,
    diff_scratch_against_project,
    run_replay,
)
from apm_cli.integration.targets import resolve_targets

_INSTR_TEMPLATE = (
    b"---\n"
    b'applyTo: "**"\n'
    b"---\n"
    b"# Rule {idx}\n"
    b"\n"
    b"Body line for primitive {idx}.\n"
    b"Another line for primitive {idx}.\n"
)


def _make_large_project(tmp_path: Path, n_primitives: int) -> Path:
    project = tmp_path / "perf-fixture"
    project.mkdir()
    (project / "apm.yml").write_bytes(
        yaml.safe_dump(
            {"name": "perf-fixture", "version": "1.0.0", "targets": ["copilot"]}
        ).encode()
    )
    # v2 target resolution needs either a signal or explicit yaml target
    # to avoid NoHarnessError (#1154).  The explicit target above and
    # .github/copilot-instructions.md signal keep this fixture on copilot
    # (matching the pre-#1154 legacy-fallback behavior).
    gi = project / ".github"
    gi.mkdir()
    (gi / "copilot-instructions.md").write_text("")
    inst_dir = project / ".apm" / "instructions"
    inst_dir.mkdir(parents=True)
    for idx in range(n_primitives):
        body = _INSTR_TEMPLATE.replace(b"{idx}", str(idx).encode())
        (inst_dir / f"rule-{idx:03d}.instructions.md").write_bytes(body)
    return project


def test_drift_replay_under_10s_for_100_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_large_project(tmp_path, n_primitives=100)
    monkeypatch.chdir(project)

    install_result = CliRunner().invoke(cli, ["install"], catch_exceptions=False)
    assert install_result.exit_code == 0, install_result.output

    targets = resolve_targets(project.resolve())
    cfg = ReplayConfig(
        project_root=project.resolve(),
        lockfile_path=project / "apm.lock.yaml",
    )
    logger = CheckLogger(verbose=False)
    lockfile = LockFile.read(cfg.lockfile_path)
    assert lockfile is not None

    start = time.perf_counter()
    scratch = run_replay(cfg, logger)
    findings = diff_scratch_against_project(scratch, project.resolve(), lockfile, targets)
    elapsed = time.perf_counter() - start

    assert findings == [], f"clean fixture must produce zero drift, got: {findings}"
    assert elapsed < 10.0, f"drift replay+diff took {elapsed:.2f}s for 100 primitives (budget: 10s)"
