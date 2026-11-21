"""APMLifecycle: install --verbose reports each Azure DevOps auth step once (#4290).

PROMISE: I9 -- no duplicate or noisy lines: a dependency the clone retries
reports the same auth-chain step once per command
(source: .agents/skills/apm-lifecycle-tests, cross-cutting invariants).
CELL: OP=install SRC=ado PRIM=skill SCOPE=project AUTH=none NET=impersonated-ado
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._ado_auth_kit import (
    AdoAuthScenario,
    ado_dependency_entry,
    describe,
    manifest,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I9"),
]


def test_verbose_ado_auth_steps_print_once(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    project = scenario.project(manifest=manifest(ado_dependency_entry()))

    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        for name in ("ADO_APM_PAT", "AZURE_DEVOPS_EXT_PAT", "SYSTEM_ACCESSTOKEN"):
            env.pop(name, None)
        result = scenario.run(
            ("install", "--verbose", "--no-policy"), cwd=project, env=env, step="install-verbose"
        )

    output = result.stdout + result.stderr
    assert result.returncode != 0, describe(result, proxy)
    assert output.count("Auth-only attempt for ado host dev.azure.com") == 1, describe(result)
