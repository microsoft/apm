"""APMLifecycle: x- vendor keys in apm-policy.yml load silently (Part of #4092).

PROMISE: req-pl-009 -- an unknown top-level policy key warns and never fails
the load; an ``x-[a-z][a-z0-9-]*`` key is ignored without a warning and never
interpreted (source: docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=policy-status SRC=file SCOPE=project GATE=org-policy NET=offline
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_POLICY = """\
name: org-policy
version: 1.0.0
x-acme-owner: security-team
enforcement: block
bogus-key: 1
dependencies:
  deny:
    - evil/*
"""


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.req("req-pl-009")
def test_policy_vendor_key_loads_without_warning(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    policy = isolated.work_root / "apm-policy.yml"
    policy.write_text(_POLICY, encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60)

    result = runner.run(
        ("policy", "status", "--check", "--policy-source", str(policy), "--json"),
        cwd=isolated.work_root,
        env=env,
    )

    assert result.returncode == 0, _evidence(result)
    data = json.loads(result.stdout)
    assert data["warnings"] == ["Unknown top-level policy key: 'bogus-key'"], _evidence(result)
    assert data["enforcement"] == "block", _evidence(result)
    assert data["rule_counts"]["dependencies_deny"] == 1, _evidence(result)
    assert "x-acme-owner" not in result.stdout + result.stderr, _evidence(result)
