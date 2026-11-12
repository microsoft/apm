"""Real installed-CLI parsing for the corrected explicit manifest identity.

Compile dry-run reads the real manifest without resolving remote packages or
starting MCP servers. This qualifies contract parsing, not install resolution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_ROOT = Path(__file__).resolve().parents[2]
_CASES = json.loads(
    (_ROOT / "tests/fixtures/spec-conformance/manifest/dependency-schema-cases.json").read_text(
        encoding="utf-8"
    )
)
_SCHEMA_URI = "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.48.schema.json"


def _project(tmp_path: Path, document: dict[str, Any]) -> tuple[IsolatedApmEnvironment, Path]:
    """Create only local inputs in the canonical credential-free environment."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(document, project / "apm.yml")
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "repro.instructions.md").write_text(
        "---\napplyTo: '**'\ndescription: Contract fixture\n---\n# Repro\nUse clear names.\n",
        encoding="utf-8",
    )
    (isolated.config_root / "config.json").write_text(
        json.dumps({"experimental": {"registries": True}}), encoding="utf-8"
    )
    return isolated, project


@pytest.mark.parametrize("section", ["dependencies", "devDependencies"])
@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_installed_cli_dependency_contract(
    apm_binary_path: Path, tmp_path: Path, section: str, case: dict[str, Any]
) -> None:
    """The installed CLI must accept positives and diagnose the paired negatives."""
    document = {
        "$schema": _SCHEMA_URI,
        "name": "schema-repro",
        "version": "0.0.0",
        "target": "copilot",
        section: {case["kind"]: [case["entry"]]},
    }
    isolated, project = _project(tmp_path, document)
    original = (project / "apm.yml").read_bytes()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=30)
    result = runner.run(
        ("compile", "--dry-run", "--local-only", "--target", "copilot"),
        scenario_id=f"schema-0148-{section}-{case['name']}",
        cwd=project,
        env=isolated.subprocess_env(),
    )
    output = result.stdout + result.stderr
    assert result.returncode == (0 if case["valid"] else 1), output
    if not case["valid"]:
        assert " ".join(case["diagnostic"].split()) in " ".join(output.split())
    assert (project / "apm.yml").read_bytes() == original
    assert not (project / "apm.lock.yaml").exists()
    assert not (project / "apm_modules").exists()


@pytest.mark.parametrize("identity", [None, "0.1", "0.1.41", "0.1.48", "9.9.9"])
def test_installed_cli_contract_identity_boundaries(
    apm_binary_path: Path, tmp_path: Path, identity: str | None
) -> None:
    """Old opt-ins and omission keep working; arbitrary IDs never fall back."""
    document = {"name": "contract-repro", "version": "0.0.0", "target": "copilot"}
    if identity is not None:
        document["$schema"] = (
            f"https://microsoft.github.io/apm/specs/schemas/manifest-v{identity}.schema.json"
        )
    isolated, project = _project(tmp_path, document)
    result = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=30).run(
        ("compile", "--dry-run", "--local-only", "--target", "copilot"),
        scenario_id=f"schema-identity-{identity}",
        cwd=project,
        env=isolated.subprocess_env(),
    )
    output = result.stdout + result.stderr
    assert result.returncode == (1 if identity == "9.9.9" else 0), output
    if identity == "9.9.9":
        assert "Unsupported apm.yml $schema contract" in output
