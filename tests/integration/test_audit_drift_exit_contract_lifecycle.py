"""APMLifecycle: plain ``apm audit`` drift severity matches its exit code (#4191).

PROMISE: I8 -- bare ``apm audit`` exits 0 on advisory drift and exits 1 only
when org policy sets ``security.audit.fail_on_drift``; an ``[x]`` drift line
appears only when the run fails (source:
docs/src/content/docs/reference/cli/audit.md, "Exit codes").
CELL: OP=audit SRC=git PRIM=skill SCOPE=project TGT=claude GATE=fail_on_drift
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    output,
    run_step,
    tree_snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
    pytest.mark.promise("P-INV-I8"),
]

_PACKAGE = "pkg-drift"
_DEPLOYED_SKILL = f".claude/skills/{_PACKAGE}-skill/SKILL.md"
_ERROR_HEADER = "[x] Drift detected: 1 file(s)"
_WARNING_HEADER = "[!] Drift detected: 1 file(s)"


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), output(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), output(result)


@pytest.mark.parametrize("fail_on_drift", [False, True], ids=["advisory", "fail-on-drift"])
def test_plain_audit_drift_line_severity_matches_exit_code(
    tmp_path: Path, apm_binary_path: Path, fail_on_drift: bool
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(
        {
            "name": "contoso-policy",
            "version": "1.0.0",
            "enforcement": "block",
            "security": {"audit": {"fail_on_drift": fail_on_drift}},
        }
    )
    package = world.publish_package(_PACKAGE)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        project = world.consumer(dependencies=(f"{package.slug}#v1.0.0",), targets=("claude",))

        def apm(*args: str, expected: int = 0, step: str) -> CommandResult:
            result = run_step(runner, args, cwd=project, env=env, step=step, expected=expected)
            _assert_output_oracles(result)
            return result

        apm("install", step="install")
        clean = apm("audit", step="audit-clean")
        assert "[+] No drift detected" in output(clean), output(clean)

        deployed = project / _DEPLOYED_SKILL
        deployed.write_text(deployed.read_text(encoding="utf-8") + "local edit\n", "utf-8")
        before = tree_snapshot(project)

        drifted = apm("audit", expected=int(fail_on_drift), step="audit-drift")

        lines = output(drifted).splitlines()
        report = drifted.stdout.splitlines()
        assert (_ERROR_HEADER in lines) is fail_on_drift, output(drifted)
        assert (_WARNING_HEADER in report) is not fail_on_drift, output(drifted)
        assert f"    - {_DEPLOYED_SKILL}  [{package.slug}]" in lines, output(drifted)
        assert tree_snapshot(project) == before, "audit must not write"

        # Execute the printed restore hint: delete the modified file, rerun install.
        hint = (
            "  [i] To restore the locked content, delete the modified files and rerun "
            "'apm install', then commit the result"
        )
        assert any(line.startswith(hint) for line in lines), output(drifted)
        deployed.unlink()
        apm("install", step="restore-install")
        restored = apm("audit", step="audit-restored")
        assert "[+] No drift detected" in output(restored), output(restored)
        assert "local edit" not in deployed.read_text(encoding="utf-8")
