"""``apm audit --ci`` and ``apm install --frozen`` agree on registry content hashes (#4187).

PROMISE: I3 -- ``install --frozen`` and ``audit --ci`` reach the same verdict
on the same lockfile state (source: .agents/skills/apm-lifecycle-tests/SKILL.md;
owner ``package-content-hash`` in .apm/architecture/owners/install-deployment.json).
CELL: OP=audit-ci|install-frozen SRC=registry PRIM=apm-package SCOPE=project
LOCK=content-hash-differs-from-locked-archive DEPLOYED=warm|cold FROZEN

A registry lock row whose ``content_hash`` does not describe the extracted
archive at its locked ``resolved_hash`` is one state with one verdict: both
readers fail with the shared entry/expected/observed line, and neither writes
the project or the lockfile.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_registry_archive_limits_lifecycle import (
    _INSTALL,
    _archive,
    _entries,
    _Scenario,
    _snapshot,
    _text,
)
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-OWN-package-content-hash"),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
]

_PACKAGE = "acme/widget"
_OTHER_HASH = "sha256:" + "0" * 64


def _set_locked_content_hash(project: Path, value: str) -> str:
    lock_path = project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    rows = [row for row in lock["dependencies"] if row.get("repo_url") == _PACKAGE]
    assert len(rows) == 1, lock["dependencies"]
    assert rows[0].get("source") == "registry", rows[0]
    recorded = rows[0]["content_hash"]
    assert recorded and recorded != value
    rows[0]["content_hash"] = value
    dump_yaml(lock, lock_path)
    return recorded


def _durable(project: Path) -> dict[str, bytes]:
    """Project files and lockfile; the apm_modules cache is not durable state."""
    return {k: v for k, v in _snapshot(project).items() if not k.startswith("apm_modules/")}


def _names_mismatch(text: str, expected: str, observed: str) -> bool:
    line = f"Content hash mismatch for {_PACKAGE}: expected {expected}, got {observed}."
    return line in re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("deployed", ["warm", "cold"])
def test_audit_ci_and_frozen_install_agree_on_registry_content_hash(
    tmp_path: Path, apm_binary_path: Path, deployed: str
) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path)
    scenario.registry.archives[_PACKAGE] = _archive("zip", _entries("widget", "valid"))
    scenario.write_manifest([f"{_PACKAGE}#1.0.0"])
    project = scenario.project
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        scenario.enable(env)
        installed = scenario.run(env, *_INSTALL, step="install")
        assert installed.returncode == 0, _text(installed)
        if deployed == "cold":
            shutil.rmtree(project / "apm_modules")
        baseline = scenario.run(env, "audit", "--ci", step="audit-baseline")
        assert baseline.returncode == 0, _text(baseline)

        recorded = _set_locked_content_hash(project, _OTHER_HASH)
        before = _durable(project)

        # I3 paired probe: both readers of the same lock state refuse it.
        audit = scenario.run(env, "audit", "--ci", step=f"audit-{deployed}")
        after_audit = _durable(project)
        frozen = scenario.run(env, "install", "--frozen", "--no-policy", step=f"frozen-{deployed}")
        after_frozen = _durable(project)

        _set_locked_content_hash(project, recorded)
        converged = [
            scenario.run(env, *args, step=f"converged-{index}")
            for index, args in enumerate(
                (("audit", "--ci"), ("install", "--frozen", "--no-policy"))
            )
        ]

    for refused in (audit, frozen):
        assert refused.returncode != 0, _text(refused)
        assert _names_mismatch(refused.stdout + refused.stderr, _OTHER_HASH, recorded), _text(
            refused
        )
    assert after_audit == before, "I5: audit --ci changed durable state"
    assert after_frozen == before, "I7: refused frozen install changed durable state"
    for result in converged:
        assert result.returncode == 0, _text(result)
