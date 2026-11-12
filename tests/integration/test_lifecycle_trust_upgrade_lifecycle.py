"""Project lifecycle trust survives a CLI upgrade, edits and new events (#3649).

Covers #3596 (pre-compile event, fresh trust for blocks naming it, non-zero
exit warning) and #3544 (non-positive timeouts) as one multi-step chain on
the real binary. A pre-0.36 trust record is simulated with the original
fingerprint domain (sha256 of the canonical JSON subtree).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import output, run_step, write
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="bash lifecycle commands"),
]


def _cmd(marker: Path, label: str, *, extra: str = "", **fields: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "type": "command",
        "bash": f"echo {label} >> '{marker}'{extra}",
    }
    entry.update(fields)
    return entry


def _fired(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return [line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line]


def _legacy_trust(isolated: IsolatedApmEnvironment, manifest: Path) -> None:
    """Write a trust record as APM <= 0.35 computed it (domain 1)."""
    lifecycle = load_yaml(manifest)["lifecycle"]
    canonical = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    store = isolated.config_root / "scripts-trust.json"
    store.write_text(
        json.dumps({"version": 1, "projects": {str(manifest.resolve()): digest}}),
        encoding="utf-8",
    )


def _project(isolated: IsolatedApmEnvironment, name: str, lifecycle: dict) -> Path:
    project = isolated.work_root / name
    project.mkdir()
    dump_yaml(
        {
            "name": name,
            "version": "1.0.0",
            "description": "Lifecycle trust fixture",
            "targets": ["copilot"],
            "dependencies": {"apm": []},
            "lifecycle": lifecycle,
        },
        project / "apm.yml",
    )
    write(
        project / ".apm" / "instructions" / "base.instructions.md",
        '---\napplyTo: "**"\ndescription: base\n---\nBase guidance.\n',
    )
    return project


def test_legacy_trust_upgrade_chain(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)
    marker = isolated.root / "fired.log"

    # Block without pre-compile: a legacy record keeps authorizing it.
    plain = _project(isolated, "plain", {"post-install": [_cmd(marker, "plain:post-install")]})
    _legacy_trust(isolated, plain / "apm.yml")
    run_step(runner, ("install",), cwd=plain, env=env, step="1-plain-install")
    assert _fired(marker) == ["plain:post-install"]
    marker.unlink()

    # Block naming pre-compile: the legacy record no longer authorizes ANY entry.
    gen = isolated.root / "generated"
    compiled = _project(
        isolated,
        "compiled",
        {
            "post-install": [_cmd(marker, "compiled:post-install")],
            "pre-compile": [_cmd(marker, "compiled:pre-compile")],
        },
    )
    _legacy_trust(isolated, compiled / "apm.yml")
    r = run_step(runner, ("install",), cwd=compiled, env=env, step="2-compiled-install")
    assert _fired(marker) == [], output(r)
    assert "apm lifecycle trust" in output(r)
    r = run_step(runner, ("compile",), cwd=compiled, env=env, step="3-compiled-compile")
    assert _fired(marker) == [], output(r)

    run_step(runner, ("lifecycle", "trust"), cwd=compiled, env=env, step="4-trust")
    run_step(runner, ("install",), cwd=compiled, env=env, step="5-install")
    assert _fired(marker) == ["compiled:post-install"]
    run_step(runner, ("compile",), cwd=compiled, env=env, step="6-compile")
    assert _fired(marker) == ["compiled:post-install", "compiled:pre-compile"]

    # pre-compile never runs for dry-run / validate.
    for args in (("compile", "--dry-run"), ("compile", "--validate")):
        run_step(runner, args, cwd=compiled, env=env, step=f"7-{args[-1]}")
    assert _fired(marker).count("compiled:pre-compile") == 1

    # Editing a non-lifecycle key keeps trust; editing lifecycle revokes it.
    manifest = load_yaml(compiled / "apm.yml")
    manifest["description"] = "edited"
    dump_yaml(manifest, compiled / "apm.yml")
    run_step(runner, ("compile",), cwd=compiled, env=env, step="8-compile-after-desc-edit")
    assert _fired(marker).count("compiled:pre-compile") == 2

    manifest["lifecycle"]["pre-compile"].append(_cmd(gen, "injected"))
    dump_yaml(manifest, compiled / "apm.yml")
    run_step(runner, ("compile",), cwd=compiled, env=env, step="9-compile-after-lc-edit")
    assert _fired(marker).count("compiled:pre-compile") == 2
    assert not gen.exists()

    # APM_NO_SCRIPTS and untrust both stop everything.
    run_step(runner, ("lifecycle", "trust"), cwd=compiled, env=env, step="10-retrust")
    no_scripts = {**env, "APM_NO_SCRIPTS": "1"}
    run_step(runner, ("install",), cwd=compiled, env=no_scripts, step="11-no-scripts")
    run_step(runner, ("compile",), cwd=compiled, env=no_scripts, step="11b-no-scripts")
    assert _fired(marker).count("compiled:pre-compile") == 2
    assert not gen.exists()
    run_step(runner, ("lifecycle", "untrust"), cwd=compiled, env=env, step="12-untrust")
    run_step(runner, ("compile",), cwd=compiled, env=env, step="13-compile-untrusted")
    assert _fired(marker).count("compiled:pre-compile") == 2


def test_failing_and_bad_timeout_scripts_are_nonfatal(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)
    marker = isolated.root / "fired.log"
    project = _project(
        isolated,
        "flaky",
        {
            "pre-install": [_cmd(marker, "pre-install", extra="; exit 3", timeoutSec=0)],
            "post-install": [_cmd(marker, "post-install", timeout=-1)],
            "pre-compile": [_cmd(marker, "pre-compile", extra="; exit 4")],
        },
    )
    run_step(runner, ("lifecycle", "trust"), cwd=project, env=env, step="0-trust")

    r = run_step(runner, ("install",), cwd=project, env=env, step="1-install")
    text = " ".join(output(r).split())
    assert _fired(marker) == ["pre-install", "post-install"], text
    assert "pre-install command script exited with code 3" in text, text
    assert "timeoutSec must be positive" in text, text
    assert "timeout must be positive" in text, text
    assert "scripts.log" in text

    r = run_step(runner, ("compile",), cwd=project, env=env, step="2-compile")
    text = " ".join(output(r).split())
    assert _fired(marker)[-1] == "pre-compile", text
    assert "pre-compile command script exited with code 4" in text, text
    assert any(
        p.name == "copilot-instructions.md" or p.suffix == ".md"
        for p in (project / ".github").rglob("*")
    ), "compile produced nothing after failing script"

    log = isolated.config_root / "logs" / "scripts.log"
    assert log.exists()
    assert "exit_code=3" in log.read_text(encoding="utf-8")
