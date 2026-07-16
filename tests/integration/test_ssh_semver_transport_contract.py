"""Real-binary Consume contract for SSH-aware git semver resolution.

Each row rewrites exactly one remote transport to a local tagged Git origin.
The isolated environment permits only ``file`` transport, so selecting the
wrong remote protocol fails before any external socket can be opened.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    assert_paths_absent,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import LifecycleAction, ScenarioObservation, ScenarioRow

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_HOST = "github.com"
_OWNER_REPO = "acme/ssh-semver-contract"
_SSH_REMOTE = f"git@{_HOST}:{_OWNER_REPO}.git"
_HTTPS_REMOTE = f"https://{_HOST}/{_OWNER_REPO}.git"
_SHORTHAND_SOURCE = _OWNER_REPO
_SKILL_NAME = "ssh-semver-contract"
_SKILL_PATH = Path(".agents") / "skills" / _SKILL_NAME / "SKILL.md"
_SEMVER_RANGE = "^1.0.0"
_INSTALL_ARGS = (
    "install",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_LOCK_ARGS = (
    "lock",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_UPDATE_ARGS = (
    "update",
    "--yes",
    "--target",
    "copilot",
    "--parallel-downloads",
    "0",
)
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")
# Frozen binaries do not import the isolated environment's sitecustomize guard.
# Fence best-effort HTTP metadata probes through a closed loopback endpoint.
_DENY_PROXY = "http://127.0.0.1:9"
_HERMETIC_ENVIRONMENT = {
    "APM_NO_CACHE": "1",
    "HTTP_PROXY": _DENY_PROXY,
    "HTTPS_PROXY": _DENY_PROXY,
    "ALL_PROXY": _DENY_PROXY,
    "NO_PROXY": "",
    "http_proxy": _DENY_PROXY,
    "https_proxy": _DENY_PROXY,
    "all_proxy": _DENY_PROXY,
    "no_proxy": "",
}


@dataclass(frozen=True)
class _TransportCase:
    id: str
    manifest_source: str
    rewritten_remote: str
    prefer_ssh: bool
    install_args: tuple[str, ...]
    expect_success: bool
    expected_transport: str


_CASES = (
    _TransportCase(
        id="explicit-ssh",
        manifest_source=_SSH_REMOTE,
        rewritten_remote=_SSH_REMOTE,
        prefer_ssh=True,
        install_args=_INSTALL_ARGS,
        expect_success=True,
        expected_transport="ssh",
    ),
    _TransportCase(
        id="prefer-ssh",
        manifest_source=_SHORTHAND_SOURCE,
        rewritten_remote=_SSH_REMOTE,
        prefer_ssh=True,
        install_args=_INSTALL_ARGS,
        expect_success=True,
        expected_transport="ssh",
    ),
    _TransportCase(
        id="https-control",
        manifest_source=_HTTPS_REMOTE,
        rewritten_remote=_HTTPS_REMOTE,
        prefer_ssh=False,
        install_args=(*_INSTALL_ARGS, "--https"),
        expect_success=True,
        expected_transport="https",
    ),
    _TransportCase(
        id="strict-ssh-negative",
        manifest_source=_SHORTHAND_SOURCE,
        rewritten_remote=_HTTPS_REMOTE,
        prefer_ssh=True,
        install_args=_INSTALL_ARGS,
        expect_success=False,
        expected_transport="ssh",
    ),
)


@dataclass(frozen=True)
class _Scenario:
    row: ScenarioRow
    environment: dict[str, str]
    project_root: Path
    trace_path: Path
    manifest_entry: dict[str, str]
    expected_commit: GitCommit
    expected_skill_bytes: bytes


def _skill_document(marker: str) -> str:
    return (
        "---\n"
        f"name: {_SKILL_NAME}\n"
        "description: SSH semver transport contract skill\n"
        "---\n"
        f"# {marker}\n"
    )


def _configure_rewrite(
    repository: LocalGitRepository,
    remote: str,
    *,
    environment: dict[str, str],
) -> None:
    subprocess.run(
        (
            "git",
            "config",
            "--global",
            "--add",
            f"url.{repository.file_url}.insteadOf",
            remote,
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _create_scenario(root: Path, case: _TransportCase) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    fixture_environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    bundle = packages.create("ssh-semver-contract", targets=("copilot",))
    skill_path = packages.add_skill(
        bundle,
        _SKILL_NAME,
        _skill_document("version one"),
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=fixture_environment,
    )
    repository = repositories.create("ssh-semver-contract", source_tree=bundle.root)
    first_commit = repositories.commit(repository, message="seed SSH semver package")
    repositories.tag(repository, "v1.0.0", first_commit)

    repository_skill = repository.worktree / skill_path.relative_to(bundle.root)
    expected_skill_bytes = _skill_document("version two").encode()
    repository_skill.write_bytes(expected_skill_bytes)
    expected_commit = repositories.commit(repository, message="advance SSH semver package")
    repositories.tag(repository, "v1.1.0", expected_commit)
    _configure_rewrite(
        repository,
        case.rewritten_remote,
        environment=fixture_environment,
    )

    manifest_entry = {
        "git": case.manifest_source,
        "ref": _SEMVER_RANGE,
    }
    consumer = LocalPackageFactory(isolated.work_root).create(
        f"consumer-{case.id}",
        dependencies=(manifest_entry,),
        targets=("copilot",),
    )
    actions = [
        LifecycleAction(
            (
                "config",
                "set",
                "prefer-ssh",
                "true" if case.prefer_ssh else "false",
            )
        ),
        LifecycleAction(
            case.install_args,
            expected_returncode=0 if case.expect_success else 1,
        ),
    ]
    if case.expect_success:
        actions.extend(
            (
                LifecycleAction(_LOCK_ARGS),
                LifecycleAction(_UPDATE_ARGS),
                LifecycleAction(_AUDIT_ARGS),
            )
        )
    row = ScenarioRow(
        id=case.id,
        source_inputs=(consumer.manifest_path, repository.origin),
        lifecycle_actions=tuple(actions),
    )
    trace_path = isolated.temp_root / "git-trace.json"
    environment = isolated.subprocess_env()
    environment.update(_HERMETIC_ENVIRONMENT)
    environment["GIT_TRACE2_EVENT"] = str(trace_path)
    return _Scenario(
        row=row,
        environment=environment,
        project_root=consumer.root,
        trace_path=trace_path,
        manifest_entry=manifest_entry,
        expected_commit=expected_commit,
        expected_skill_bytes=expected_skill_bytes,
    )


def _run_scenario(
    scenario: _Scenario,
    *,
    apm_binary_path: Path,
) -> ScenarioObservation:
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=360,
    )
    results = []
    snapshots = [ArtifactSnapshot.capture(scenario.project_root)]
    for index, action in enumerate(scenario.row.lifecycle_actions):
        result = runner.run(
            action.args,
            scenario_id=f"{scenario.row.id}-{index}",
            cwd=scenario.project_root,
            env=scenario.environment,
        )
        assert result.returncode == action.expected_returncode, (
            f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        results.append(result)
        snapshots.append(ArtifactSnapshot.capture(scenario.project_root))
    return ScenarioObservation(
        source_inputs=scenario.row.source_inputs,
        results=tuple(results),
        snapshots=tuple(snapshots),
    )


def _transport_for_argument(argument: str) -> str | None:
    if argument == _SSH_REMOTE:
        return "ssh"
    parsed = urlparse(argument)
    expected = urlparse(_HTTPS_REMOTE)
    if (
        parsed.scheme,
        parsed.hostname,
        parsed.path.rstrip("/"),
    ) == (
        expected.scheme,
        expected.hostname,
        expected.path.rstrip("/"),
    ):
        return "https"
    return None


def _invoked_transports(trace_path: Path) -> tuple[str, ...]:
    transports = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") != "start":
            continue
        arguments = tuple(str(argument) for argument in event.get("argv", ()))
        if "ls-remote" not in arguments or "--tags" not in arguments:
            continue
        for argument in arguments:
            transport = _transport_for_argument(str(argument))
            if transport is not None:
                transports.append(transport)
    return tuple(transports)


def _locked_dependency(project_root: Path) -> dict[str, object]:
    lock = load_yaml(project_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


def _assert_success_contract(
    scenario: _Scenario,
    observation: ScenarioObservation,
) -> None:
    locked = _locked_dependency(scenario.project_root)
    assert locked["host"] == _HOST
    assert locked["repo_url"] == _OWNER_REPO
    assert locked["constraint"] == _SEMVER_RANGE
    assert locked["resolved_tag"] == "v1.1.0"
    assert locked["resolved_ref"] == "v1.1.0"
    assert locked["version"] == "1.1.0"
    assert locked["resolved_commit"] == scenario.expected_commit.sha
    assert locked["resolved_at"]
    assert locked.get("source") is None

    manifest = load_yaml(scenario.project_root / "apm.yml")
    assert manifest["dependencies"]["apm"] == [scenario.manifest_entry]
    deployed = scenario.project_root / _SKILL_PATH
    assert deployed.read_bytes() == scenario.expected_skill_bytes
    deployed_path = _SKILL_PATH.as_posix()
    assert deployed_path in locked["deployed_files"]
    expected_hash = f"sha256:{hashlib.sha256(scenario.expected_skill_bytes).hexdigest()}"
    assert locked["deployed_file_hashes"][deployed_path] == expected_hash
    assert str(locked["content_hash"]).startswith("sha256:")

    update_result = observation.results[-2]
    update_output = update_result.stdout + update_result.stderr
    assert "All dependencies already at their latest matching refs." in update_output
    assert not any(line.startswith("  [~] ") for line in update_output.splitlines())
    audit_payload = json.loads(observation.results[-1].stdout)
    assert audit_payload["passed"] is True
    assert audit_payload["summary"]["failed"] == 0

    after_lock = observation.snapshots[-3]
    assert_unchanged(after_lock, observation.snapshots[-2])
    assert_unchanged(after_lock, observation.snapshots[-1])


def _assert_failure_contract(
    scenario: _Scenario,
    observation: ScenarioObservation,
) -> None:
    output = observation.results[-1].stdout + observation.results[-1].stderr
    assert "Failed to download dependency" in " ".join(output.split())
    assert "No install transaction changes were committed." in " ".join(output.split())
    final_snapshot = observation.snapshots[-1]
    assert_paths_absent(
        final_snapshot,
        {
            "apm.lock.yaml",
            _SKILL_PATH.as_posix(),
        },
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.id)
def test_git_semver_resolution_honors_consume_transport_contract(
    tmp_path: Path,
    apm_binary_path: Path,
    case: _TransportCase,
) -> None:
    scenario = _create_scenario(tmp_path / case.id, case)
    assert scenario.environment["GIT_ALLOW_PROTOCOL"] == "file"
    deny_proxy = urlparse(scenario.environment["HTTPS_PROXY"])
    assert (deny_proxy.hostname, deny_proxy.port) == ("127.0.0.1", 9)
    assert scenario.environment["NO_PROXY"] == ""
    assert scenario.environment["APM_NO_CACHE"] == "1"
    observation = _run_scenario(scenario, apm_binary_path=apm_binary_path)

    if case.expect_success:
        _assert_success_contract(scenario, observation)
    else:
        _assert_failure_contract(scenario, observation)

    transports = _invoked_transports(scenario.trace_path)
    assert transports
    assert set(transports) == {case.expected_transport}
