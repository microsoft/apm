"""APMLifecycle: a failing `apm audit --ci` in json or sarif names its failure (#4224).

PROMISE: I7 -- nothing fails silently: a structured report keeps stdout (or
the file) machine-readable and states the failed check on stderr, as the
text format does (source: .agents/skills/apm-lifecycle-tests).
CELL: OP=audit-ci FORMAT=sarif|json OUTPUT=file|stdout LOCK=missing
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]


@pytest.mark.parametrize(("fmt", "to_file"), [("sarif", True), ("sarif", False), ("json", True)])
def test_failing_structured_audit_names_the_failed_check_on_stderr(
    tmp_path: Path, apm_binary_path: Path, fmt: str, to_file: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: x\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/pkg#v1.0.0\n",
        encoding="utf-8",
    )
    report = project / f"report.{fmt}"
    args = ("audit", "--ci", "--no-policy", "-f", fmt, *(("-o", str(report)) if to_file else ()))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = runner.run(args, cwd=project, env=isolated.subprocess_env())

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[x] lockfile-exists:" in result.stderr, result.stderr
    assert "[x] 1 of 1 check(s) failed" in result.stderr, result.stderr
    document = report.read_text(encoding="utf-8") if to_file else result.stdout
    json.loads(document)
    if to_file:
        assert result.stdout == "", result.stdout
