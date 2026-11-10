"""Installed-CLI frozen native-service replay using generated ownership baselines."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_intellij_mcp_config_lifecycle import _intellij_paths
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _publish,
    _result_evidence,
    _Scenario,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.local_package import LocalPackage

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_MCP = {
    "name": "frozen-service",
    "registry": False,
    "transport": "http",
    "url": "https://example.invalid/frozen",
}
_LSP = {
    "name": "frozen-language",
    "command": "inert-language-server",
    "extensionToLanguage": {".fixture": "fixture"},
}


def _run(
    scenario: _Scenario,
    project: LocalPackage,
    *args: str,
    label: str,
) -> CommandResult:
    """Run the actual installed entry point without activating native clients."""
    return scenario.runner.run(
        (*_INSTALL_ARGS, *args),
        cwd=project.root,
        env=scenario.environment,
        scenario_id=label,
    )


def _success(scenario: _Scenario, project: LocalPackage, *args: str, label: str) -> None:
    result = _run(scenario, project, *args, label=label)
    assert result.returncode == 0, _result_evidence(result)


def _baseline(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    package: bool = False,
    lsp: bool = False,
    targets: tuple[str, ...] = ("copilot", "claude"),
) -> tuple[_Scenario, LocalPackage]:
    scenario = _new_scenario(tmp_path / "native frozen roots", apm_binary_path)
    dependencies = ()
    if package:
        published = _publish(scenario, "frozen-source", skill="frozen-skill")
        scenario.environment.update(published.environment)
        dependencies = (published.dependency,)
    project = scenario.consumers.create(
        "frozen-consumer",
        dependencies=dependencies,
        mcp_dependencies=(_MCP,),
        lsp_dependencies=(_LSP,) if lsp else (),
        targets=targets,
    )
    intellij, _, _ = _intellij_paths(scenario.isolated)
    intellij.parent.mkdir(parents=True, exist_ok=True)
    intellij.write_text(
        json.dumps({"setting": "keep", "servers": {"personal": {"command": "do-not-run"}}}),
        encoding="ascii",
    )
    _success(scenario, project, label="generate-baseline")
    lock = project.root / "apm.lock.yaml"
    lock.write_bytes(
        b"# Committed legacy metadata must survive frozen replay.\n"
        + lock.read_bytes()
        + b"generated_at: '2020-01-02T03:04:05Z'\n"
    )
    return scenario, project


def _native_bytes(scenario: _Scenario, project: LocalPackage) -> dict[str, bytes | None]:
    """Independent native paths, including absent paths, catch writes and removals."""
    intellij, legacy, _ = _intellij_paths(scenario.isolated)
    paths = [
        project.root / ".github" / "mcp.json",
        project.root / ".vscode" / "mcp.json",
        project.root / ".mcp.json",
        project.root / ".github" / "lsp.json",
        project.root / ".claude" / "skills" / "apm-lsp" / ".claude-plugin" / "plugin.json",
        intellij,
    ]
    if legacy is not None:
        paths.append(legacy)
    return {str(path): path.read_bytes() if path.exists() else None for path in paths}


@pytest.mark.parametrize("package", [False, True], ids=["mcp-only", "package-and-mcp"])
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_frozen_extra_intellij_refuses_before_native_writes_and_recovers(
    tmp_path: Path, apm_binary_path: Path, package: bool, cold: bool
) -> None:
    scenario, project = _baseline(tmp_path, apm_binary_path, package=package)
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    pins = load_yaml(lock)["dependencies"] if package else {}
    before = _native_bytes(scenario, project)
    if cold and (project.root / "apm_modules").exists():
        shutil.rmtree(project.root / "apm_modules")
    result = _run(
        scenario,
        project,
        "--frozen",
        "--target",
        "copilot,claude,intellij",
        label="refuse-unrecorded-intellij",
    )
    assert result.returncode == 1, _result_evidence(result)
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert _native_bytes(scenario, project) == before
    text = " ".join((result.stdout + result.stderr).split())
    assert "intellij" in text and "frozen-service" in text, text
    assert "without --frozen" in text and "--target" in text, text
    text.encode("ascii")
    _success(
        scenario, project, "--target", "copilot,claude,intellij", label="record-intellij-normally"
    )
    repaired = load_yaml(lock)
    assert "frozen-service" in repaired["mcp_target_servers"]["intellij"]
    if package:
        assert repaired["dependencies"] == pins
    repaired_bytes = lock.read_bytes()
    for replay in range(2):
        native = _native_bytes(scenario, project)
        _success(
            scenario,
            project,
            "--frozen",
            "--target",
            "intellij,claude,copilot",
            label=f"replay-recorded-intellij-{replay}",
        )
        assert lock.read_bytes() == repaired_bytes
        assert project.manifest_path.read_bytes() == manifest
        assert _native_bytes(scenario, project) == native
    intellij, _, _ = _intellij_paths(scenario.isolated)
    assert json.loads(intellij.read_bytes())["servers"]["personal"] == {"command": "do-not-run"}


@pytest.mark.parametrize("package", [False, True], ids=["mcp-only", "package-and-mcp"])
def test_frozen_same_repeated_and_narrower_service_replays_preserve_lock_bytes(
    tmp_path: Path, apm_binary_path: Path, package: bool
) -> None:
    scenario, project = _baseline(tmp_path, apm_binary_path, package=package, lsp=True)
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    for index, target in enumerate(
        ("claude,copilot", "copilot,claude", "claude", "claude", "copilot,claude")
    ):
        _success(scenario, project, "--frozen", "--target", target, label=f"replay-{index}")
        assert lock.read_bytes() == locked
        assert project.manifest_path.read_bytes() == manifest
    assert (
        "frozen-language"
        in json.loads(
            (project.root / ".claude/skills/apm-lsp/.claude-plugin/plugin.json").read_bytes()
        )["lspServers"]
    )


def test_frozen_lsp_unrecorded_target_blocks_coupled_mcp_before_writes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario, project = _baseline(tmp_path, apm_binary_path, lsp=True, targets=("claude",))
    _success(
        scenario, project, "--only", "mcp", "--target", "copilot,claude", label="record-mcp-only"
    )
    # An early MCP write would recreate this file before the later LSP refusal.
    (project.root / ".github" / "mcp.json").unlink()
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    before = _native_bytes(scenario, project)
    result = _run(
        scenario, project, "--frozen", "--target", "copilot,claude", label="refuse-lsp-claim"
    )
    assert result.returncode == 1, _result_evidence(result)
    assert _native_bytes(scenario, project) == before
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    text = " ".join((result.stdout + result.stderr).split())
    assert "copilot" in text and "frozen-language" in text and "LSP" in text, text
    _success(scenario, project, "--target", "copilot,claude", label="record-lsp-normally")
    repaired = lock.read_bytes()
    _success(scenario, project, "--frozen", "--target", "copilot,claude", label="replay-lsp")
    assert lock.read_bytes() == repaired


def test_frozen_direct_package_services_keep_trust_and_recorded_ownership(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "transitive", apm_binary_path)
    published = _publish(scenario, "transitive-services", mcp=True)
    scenario.environment.update(published.environment)
    project = scenario.consumers.create(
        "transitive-consumer", dependencies=(published.dependency,), targets=("copilot", "claude")
    )
    _success(scenario, project, "--trust-transitive-mcp", label="trusted-baseline")
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    native = _native_bytes(scenario, project)
    direct = _run(scenario, project, "--frozen", label="direct-package-trust")
    assert direct.returncode == 0, _result_evidence(direct)
    assert _native_bytes(scenario, project) == native
    _success(
        scenario, project, "--frozen", "--trust-transitive-mcp", label="trusted-recorded-replay"
    )
    extra = _run(
        scenario,
        project,
        "--frozen",
        "--trust-transitive-mcp",
        "--target",
        "copilot,claude,intellij",
        label="trusted-but-unrecorded-target",
    )
    assert extra.returncode == 1, _result_evidence(extra)
    text = " ".join((extra.stdout + extra.stderr).split())
    assert "intellij" in text and "fixture-mcp" in text, text
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert _native_bytes(scenario, project) == native


def test_frozen_service_only_missing_package_source_cannot_remove_native_servers(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "missing-service-source", apm_binary_path)
    published = _publish(scenario, "package-services", mcp=True)
    scenario.environment.update(published.environment)
    project = scenario.consumers.create(
        "service-consumer", dependencies=(published.dependency,), targets=("copilot", "claude")
    )
    _success(scenario, project, label="package-service-baseline")
    assert (project.root / ".github" / "mcp.json").is_file()
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    native = _native_bytes(scenario, project)
    shutil.rmtree(project.root / "apm_modules")
    result = _run(scenario, project, "--only", "mcp", "--frozen", label="no-source-is-not-empty")
    assert result.returncode == 1, _result_evidence(result)
    assert _native_bytes(scenario, project) == native
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert "fixture-mcp" in result.stdout + result.stderr


def test_selected_package_install_retains_unselected_service_evidence(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Package selection preserves other services and permits frozen replay."""
    scenario = _new_scenario(tmp_path / "selected-service-graph", apm_binary_path)
    selected = _publish(scenario, "selected", skill="selected-skill")
    service = _publish(scenario, "retained-service", mcp=True)
    # Named installs validate this controlled Git route with real ls-remote.
    selected_url = "https://git.example.invalid/apm-fixture-org/selected"
    scenario.environment.update(
        scenario.repositories.url_rewrite_subprocess_env_many(
            (
                (selected.repository, selected_url),
                (service.repository, service.remote_url),
            )
        )
    )
    project = scenario.consumers.create(
        "selected-consumer",
        dependencies=({**selected.dependency, "git": selected_url}, service.dependency),
        targets=("copilot", "claude"),
    )
    _success(scenario, project, label="complete-graph-baseline")
    lock = project.root / "apm.lock.yaml"
    native = _native_bytes(scenario, project)
    assert set(json.loads((project.root / ".github/mcp.json").read_bytes())["mcpServers"]) == {
        "fixture-mcp"
    }
    trace = tmp_path / "selected-preflight.jsonl"
    scenario.environment["GIT_TRACE2_EVENT"] = str(trace)
    _success(
        scenario,
        project,
        f"{selected_url}#{selected.commit.sha}",
        label="selected-package-keeps-full-service-graph",
    )
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    probes = [
        event["argv"]
        for event in events
        if event["event"] == "start" and "ls-remote" in event["argv"]
    ]
    assert probes, events
    assert any(
        urlparse(argument).hostname == "git.example.invalid"
        and urlparse(argument).path.removesuffix(".git") == "/apm-fixture-org/selected"
        for argv in probes
        for argument in argv
    )
    scenario.environment.pop("GIT_TRACE2_EVENT")
    assert _native_bytes(scenario, project) == native
    assert {dep["resolved_commit"] for dep in load_yaml(lock)["dependencies"]} == {
        selected.commit.sha,
        service.commit.sha,
    }
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    _success(scenario, project, "--frozen", label="frozen-complete-service-graph")
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert _native_bytes(scenario, project) == native


def test_frozen_indirect_self_defined_service_still_requires_explicit_trust(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _new_scenario(tmp_path / "indirect-consent", apm_binary_path)
    leaf = _publish(scenario, "indirect-service", mcp=True)
    parent = scenario.sources.create("service-parent", dependencies=(leaf.dependency,))
    repository = scenario.repositories.create(parent.name, source_tree=parent.root)
    commit = scenario.repositories.commit(repository, message="indirect service declaration")
    remote = f"https://github.com/apm-fixture-org/{parent.name}"
    scenario.environment.update(
        scenario.repositories.url_rewrite_subprocess_env_many(
            ((leaf.repository, leaf.remote_url), (repository, remote))
        )
    )
    project = scenario.consumers.create(
        "indirect-consumer",
        dependencies=({"git": remote, "ref": commit.sha},),
        targets=("copilot", "claude"),
    )
    _success(scenario, project, "--trust-transitive-mcp", label="indirect-consent-baseline")
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    native = _native_bytes(scenario, project)
    refusal = _run(scenario, project, "--frozen", label="indirect-consent-required")
    assert refusal.returncode == 1, _result_evidence(refusal)
    assert "fixture-mcp" in refusal.stdout + refusal.stderr
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert _native_bytes(scenario, project) == native
    _success(
        scenario, project, "--frozen", "--trust-transitive-mcp", label="indirect-consent-replay"
    )
    assert lock.read_bytes() == locked
    assert _native_bytes(scenario, project) == native


@pytest.mark.parametrize("scope", ["root", "global"])
def test_frozen_scoped_mcp_only_replay_and_refusal_preserve_selected_store(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    scenario = _new_scenario(tmp_path / scope, apm_binary_path)
    source = scenario.consumers.create(
        "scoped-services", mcp_dependencies=(_MCP,), targets=("copilot", "claude")
    )
    if scope == "global":
        store = scenario.isolated.config_root
        manifest = store / "apm.yml"
        manifest.write_bytes(source.manifest_path.read_bytes())
        options = ("--global",)
    else:
        store = scenario.isolated.work_root / "deployment root"
        manifest = source.manifest_path
        options = ("--root", str(store))
    _success(scenario, source, *options, "--only", "mcp", label="scoped-baseline")
    lock = store / "apm.lock.yaml"
    locked, declared = lock.read_bytes(), manifest.read_bytes()
    _success(scenario, source, *options, "--only", "mcp", "--frozen", label="scoped-replay")
    assert lock.read_bytes() == locked
    # Capture native files under all isolated roots without relying on the writer's path logic.
    native = {
        str(path): path.read_bytes()
        for path in scenario.isolated.root.rglob("*.json")
        if "network_guard" not in path.parts
    }
    refusal = _run(
        scenario,
        source,
        *options,
        "--only",
        "mcp",
        "--frozen",
        "--target",
        "copilot,claude,intellij",
        label="scoped-refusal",
    )
    assert refusal.returncode == 1, _result_evidence(refusal)
    assert lock.read_bytes() == locked
    assert manifest.read_bytes() == declared
    assert {
        str(path): path.read_bytes()
        for path in scenario.isolated.root.rglob("*.json")
        if "network_guard" not in path.parts
    } == native
    assert f"--{scope}" in refusal.stdout + refusal.stderr
    _success(
        scenario,
        source,
        *options,
        "--only",
        "mcp",
        "--target",
        "copilot,claude,intellij",
        label="scoped-recovery",
    )
    repaired = lock.read_bytes()
    _success(
        scenario,
        source,
        *options,
        "--only",
        "mcp",
        "--frozen",
        "--target",
        "copilot,claude,intellij",
        label="scoped-recovered-replay",
    )
    assert lock.read_bytes() == repaired
    assert manifest.read_bytes() == declared


def test_frozen_pin_drift_and_audit_content_drift_remain_failures(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario, project = _baseline(tmp_path, apm_binary_path, package=True)
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    native = _native_bytes(scenario, project)
    document = load_yaml(project.manifest_path)
    document["dependencies"]["apm"][0]["ref"] = "f" * 40
    dump_yaml(document, project.manifest_path)
    changed_manifest = project.manifest_path.read_bytes()
    refusal = _run(scenario, project, "--frozen", label="real-pin-drift")
    assert refusal.returncode == 1, _result_evidence(refusal)
    assert "out of sync" in refusal.stdout + refusal.stderr
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == changed_manifest
    assert _native_bytes(scenario, project) == native
    project.manifest_path.write_bytes(manifest)
    skill = project.root / ".claude" / "skills" / "frozen-skill" / "SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\nactual deployed content drift\n")
    audit = scenario.runner.run(
        ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"),
        cwd=project.root,
        env=scenario.environment,
        scenario_id="real-content-drift",
    )
    assert audit.returncode != 0, _result_evidence(audit)
    assert "content-integrity" in audit.stdout + audit.stderr
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest


def test_frozen_real_mcp_config_drift_still_refuses_without_native_changes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario, project = _baseline(tmp_path, apm_binary_path)
    document = load_yaml(project.manifest_path)
    document["dependencies"]["mcp"][0]["url"] = "https://changed.invalid/mcp"
    dump_yaml(document, project.manifest_path)
    lock = project.root / "apm.lock.yaml"
    locked, manifest = lock.read_bytes(), project.manifest_path.read_bytes()
    before = _native_bytes(scenario, project)
    result = _run(scenario, project, "--frozen", label="real-config-drift")
    assert result.returncode == 1, _result_evidence(result)
    assert "config differs" in result.stdout + result.stderr
    assert lock.read_bytes() == locked
    assert project.manifest_path.read_bytes() == manifest
    assert _native_bytes(scenario, project) == before
