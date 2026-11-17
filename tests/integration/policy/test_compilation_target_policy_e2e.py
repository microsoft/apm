"""Real-binary contract for #3732: compilation-target policy judges the
canonical target set in ``apm audit`` and ``apm install``.

Every manifest spelling (plural ``targets:``, CSV ``target:``, the ``vscode``
alias, legacy ``targets: [all]``) is projected to the profiles install
deploys before it meets ``compilation.target.allow``. Audit therefore agrees
with the install gate instead of reading only the singular key.

Policy reaches ``install`` the way the Govern contract does: an ADO-shaped
origin, a leaf that ``extends`` a strict local policy, and ``apm policy
status`` warming the cache. Audit reads the same strict file with
``--policy``. No network is used.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_POLICY_SOURCE = "dev.azure.com/contoso/apm/apm-policy"
_POLICY_REMOTE = "https://dev.azure.com/contoso/project/_git/compilation-target"
_POLICY_DIAGNOSTIC = f"Policy: org:{_POLICY_SOURCE} (cached"
_STRICT = PurePosixPath(".apm-policy") / "strict.yml"
_AUDIT = (
    "audit",
    "--ci",
    "--no-fail-fast",
    "--no-drift",
    "--format",
    "json",
    "--policy",
    _STRICT.as_posix(),
)
_INSTRUCTION = "---\napplyTo: '**'\ndescription: compilation-target contract\n---\n# rule\n"
_ECHO_MCP = {
    "name": "local-echo",
    "registry": False,
    "transport": "stdio",
    "command": "echo",
    "args": ["hi"],
}


def _git(args: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=30)


def _project(root: Path, name: str, declaration: dict, env: dict[str, str], **extra) -> Path:
    project = root / name
    (project / ".apm" / "instructions").mkdir(parents=True)
    (project / ".apm" / "instructions" / "rule.instructions.md").write_text(_INSTRUCTION)
    dump_yaml({"name": name, "version": "1.0.0", **declaration, **extra}, project / "apm.yml")
    _git(("git", "init", "--initial-branch=main"), cwd=project, env=env)
    _git(("git", "remote", "add", "origin", _POLICY_REMOTE), cwd=project, env=env)
    strict = project.joinpath(*_STRICT.parts)
    strict.parent.mkdir()
    dump_yaml(
        {
            "name": "compilation-target-contract",
            "version": "1",
            "enforcement": "block",
            "fetch_failure": "block",
            "compilation": {"target": {"allow": ["claude", "copilot"]}},
        },
        strict,
    )
    leaf = project.joinpath(*PurePosixPath(_POLICY_SOURCE).parts)
    leaf.parent.mkdir(parents=True)
    dump_yaml({"name": "compilation-target-leaf", "extends": _STRICT.as_posix()}, leaf)
    return project


def _run(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    scenario_id: str,
    expected: int,
) -> CommandResult:
    result = runner.run(args, scenario_id=scenario_id, cwd=cwd, env=env)
    assert result.returncode == expected, (
        f"{scenario_id}: expected exit {expected}, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


def _compilation_target(runner, project: Path, env, tag: str, expected: int) -> dict:
    manifest_before = (project / "apm.yml").read_bytes()
    audit = _run(
        runner, _AUDIT, cwd=project, env=env, scenario_id=f"{tag}-audit", expected=expected
    )
    payload = json.loads(audit.stdout)
    assert payload["policy"]["enforced"] is True, payload["policy"]
    assert (project / "apm.yml").read_bytes() == manifest_before
    checks = {check["name"]: check for check in payload["checks"]}
    return checks["compilation-target"]


def _install(runner, project: Path, env, tag: str, expected: int) -> CommandResult:
    _run(
        runner,
        ("policy", "status", "--policy-source", _POLICY_SOURCE, "--json", "--check", "--no-cache"),
        cwd=project,
        env=env,
        scenario_id=f"{tag}-warm-policy",
        expected=0,
    )
    result = _run(
        runner,
        ("install", "--parallel-downloads", "0"),
        cwd=project,
        env=env,
        scenario_id=f"{tag}-install",
        expected=expected,
    )
    assert _POLICY_DIAGNOSTIC in result.stdout + result.stderr
    return result


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    root = tmp_path_factory.mktemp("compilation-target")
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    return isolated, isolated.subprocess_env()


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=600
    )


@pytest.mark.parametrize(
    ("tag", "declaration", "passed"),
    [
        pytest.param("plural-violation", {"targets": ["claude", "cursor"]}, False, id="L1"),
        pytest.param("csv-compliant", {"target": "claude,copilot"}, True, id="L2"),
        pytest.param("alias-compliant", {"target": "vscode"}, True, id="L3"),
        pytest.param("legacy-all", {"targets": ["all"]}, True, id="L4"),
    ],
)
def test_audit_judges_the_canonical_declared_targets(
    harness, apm_binary_path, tag, declaration, passed
):
    isolated, env = harness
    runner = _runner(apm_binary_path)
    project = _project(isolated.work_root, tag, declaration, env)

    check = _compilation_target(runner, project, env, tag, expected=0 if passed else 1)

    assert check["passed"] is passed, check
    if not passed:
        assert "cursor" in check["message"], check
    assert not (project / "apm.lock.yaml").exists()


def test_install_blocks_a_noncompliant_plural_declaration_before_writing(harness, apm_binary_path):
    isolated, env = harness
    runner = _runner(apm_binary_path)
    project = _project(isolated.work_root, "plural-install", {"targets": ["claude", "cursor"]}, env)

    result = _install(runner, project, env, "L5", expected=1)

    assert "compilation target" in (result.stdout + result.stderr).lower()
    assert not (project / "apm.lock.yaml").exists()
    for root in (".claude", ".cursor", ".github"):
        written = [p for p in (project / root).rglob("*") if p.is_file()]
        assert written == [], written


def test_vscode_alias_complies_and_keeps_its_mcp_destination(harness, apm_binary_path):
    isolated, env = harness
    runner = _runner(apm_binary_path)
    project = _project(
        isolated.work_root,
        "alias-install",
        {"targets": ["vscode"]},
        env,
        dependencies={"mcp": [_ECHO_MCP]},
    )

    _install(runner, project, env, "L3-install", expected=0)

    assert (project / ".vscode" / "mcp.json").is_file()
    assert not (project / ".github" / "mcp.json").exists()
    assert _compilation_target(runner, project, env, "L3-after", expected=0)["passed"] is True
