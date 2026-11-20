"""Executable proofs for install-path remediation advice (#4090).

Each proof induces the state one ``core/remediation.py`` entry clears, observes
the installed CLI report it with that entry's advice, runs exactly the advised
command (after any ``before`` step) and observes the state gone.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from apm_cli import config as apm_config
from apm_cli.cli import cli
from apm_cli.models.manifest_contract import SOURCE_REPLACEMENTS_V1_SCHEMA_URI
from apm_cli.utils import path_security
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _instruction,
    _new_scenario,
    _publish,
    _publish_legacy_plugin,
    _PublishedPackage,
    _result_evidence,
    _Scenario,
    _skill,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.remediation_proof import (
    ProofWorld,
    advised,
    flat,
    installed_world,
    run_advised,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

PROVES = (
    "resolve-dependencies",
    "retry-install",
    "rerun-after-fix",
    "apply-replacement-table",
    "enable-experimental",
    "set-cowork-skills-dir",
    "install-cowork-global",
    "compile-root-context",
    "compile-user-root-context",
    "clean-download-cache",
    "accept-new-content",
    "clean-deps-cache",
    "prune-package-copies",
    "reset-mcp-registry-url",
)

COMPONENT_PROOFS = {
    "prune-package-copies": (
        "removing a dropped dependency copy only fails on an OSError (a file held open "
        "on Windows, a permission denial), which a root test process cannot induce"
    ),
}


_INSTRUCTION = ".github/instructions/proof.instructions.md"


def _out(result: CommandResult) -> str:
    return flat(f"{result.stdout}\n{result.stderr}")


def _run_failing(world: ProofWorld, args: tuple[str, ...], scenario_id: str) -> str:
    result = world.run(args, scenario_id=scenario_id, expect=None)
    assert result.returncode != 0, _result_evidence(result)
    return _out(result)


def _publish_native_skill(scenario: _Scenario, name: str) -> _PublishedPackage:
    """Publish a package whose root ``SKILL.md`` is the deployable skill."""
    package = scenario.sources.create(name)
    (package.root / "SKILL.md").write_text(_skill(name), encoding="ascii")
    repository = scenario.repositories.create(name, source_tree=package.root)
    commit = scenario.repositories.commit(repository, message=f"seed {name}")
    remote_url = f"https://github.com/apm-fixture-org/{name}"
    return _PublishedPackage(
        package=package,
        repository=repository,
        commit=commit,
        remote_url=remote_url,
        dependency={"git": remote_url, "ref": commit.sha, "alias": name},
        environment=scenario.repositories.url_rewrite_subprocess_env(repository, remote_url),
    )


def _user_manifest(scenario: _Scenario, *dependencies: dict[str, object]) -> None:
    dump_yaml(
        {"name": "user", "version": "0.1.0", "dependencies": {"apm": list(dependencies)}},
        scenario.isolated.config_root / "apm.yml",
    )


def _cowork_world(tmp_path: Path, apm_binary_path: Path, name: str) -> ProofWorld:
    """A consumer plus a user manifest declaring one native-skill package."""
    world = installed_world(tmp_path, apm_binary_path, name=name, install=False)
    skill = _publish_native_skill(world.scenario, f"{name}-cowork")
    _user_manifest(world.scenario, skill.dependency)
    world.env.update(skill.environment)
    world.run(("experimental", "enable", "copilot-cowork"), scenario_id=f"{name}-enable")
    return world


def _locked_row(world: ProofWorld) -> tuple[dict[str, object], dict[str, object]]:
    lock = load_yaml(world.root / "apm.lock.yaml")
    rows = [row for row in lock["dependencies"] if row.get("alias") == world.source.package.name]
    assert len(rows) == 1, lock
    return lock, rows[0]


def _deployment_targets(world: ProofWorld) -> set[str]:
    lock = load_yaml(world.root / "apm.lock.yaml")
    return {row["target"] for row in lock.get("deployments") or ()}


def _dep_key(world: ProofWorld) -> str:
    return urlparse(world.source.remote_url).path.strip("/")


# --- resolve-dependencies --------------------------------------------------


def test_resolve_dependencies(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    preview = ("install", "--dry-run", "--no-policy")
    reported = _out(world.run(preview, scenario_id="dry-run-incomplete"))
    assert "Dependency preview incomplete" in reported
    remediation = advised("resolve-dependencies", reported)

    run_advised(world, remediation)
    assert (world.root / _INSTRUCTION).is_file()
    cleared = _out(world.run(preview, scenario_id="dry-run-complete"))
    assert "Dependency preview incomplete" not in cleared
    assert "resolve the dependencies" not in cleared


# --- retry-install ---------------------------------------------------------


def test_retry_install(tmp_path: Path, apm_binary_path: Path) -> None:
    """A hook reconcile interrupted by a half-written settings file is retried."""
    world = installed_world(
        tmp_path, apm_binary_path, targets=("copilot", "claude"), hook_command="echo proof"
    )
    settings = world.root / ".claude" / "settings.json"
    assert "echo proof" in settings.read_text(encoding="utf-8")
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"][0]["targets"] = ["copilot"]
    dump_yaml(manifest, world.consumer.manifest_path)

    complete = settings.read_bytes()
    settings.write_bytes(complete[: len(complete) // 2])
    reported = _out(world.install("--verbose", scenario_id="reconcile-interrupted"))
    assert "Could not fully reconcile hooks excluded by target restrictions" in reported
    remediation = advised("retry-install", reported)

    settings.write_bytes(complete)  # the concurrent writer finished
    cleared = _out(run_advised(world, remediation))
    assert "Could not fully reconcile" not in cleared
    assert "echo proof" not in settings.read_text(encoding="utf-8")
    copilot_hooks = (world.root / ".github" / "hooks").glob("*.json")
    assert any("echo proof" in path.read_text("utf-8") for path in copilot_hooks)


# --- rerun-after-fix -------------------------------------------------------


def test_rerun_after_fix(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    factory = world.scenario.consumers
    local = factory.create("local-kit")
    factory.add_instruction(local, "local", _instruction("local"))
    factory.add_relative_dependency(world.consumer, local)
    moved = local.root.with_name("local-kit-moved")
    local.root.rename(moved)

    reported = _run_failing(world, _INSTALL_ARGS, "local-source-missing")
    assert "Local package path does not exist" in reported
    remediation = advised(
        "rerun-after-fix", reported, fix="restore the source or correct the dependency path"
    )

    moved.rename(local.root)  # before: restore the source
    run_advised(world, remediation)
    assert (world.root / ".github" / "instructions" / "local.instructions.md").is_file()


# --- apply-replacement-table -----------------------------------------------


def test_apply_replacement_table(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["$schema"] = SOURCE_REPLACEMENTS_V1_SCHEMA_URI
    manifest["replace"] = []
    dump_yaml(manifest, world.consumer.manifest_path)
    partial = (*_INSTALL_ARGS, world.source.remote_url)

    reported = _run_failing(world, partial, "partial-replacements-changed")
    assert "Replacement rules changed during a partial install" in reported
    remediation = advised("apply-replacement-table", reported)

    run_advised(world, remediation)
    assert load_yaml(world.root / "apm.lock.yaml")["replacements"] == []
    world.run(partial, scenario_id="partial-after-table")


# --- enable-experimental ---------------------------------------------------


def test_enable_experimental(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, skill="proof-skill")
    install = (*_INSTALL_ARGS, "--target", "openclaw")
    reported = _out(world.run(install, scenario_id="openclaw-gated"))
    assert "The 'openclaw' target requires an experimental flag" in reported
    remediation = advised("enable-experimental", reported, flag="openclaw")
    assert "openclaw" not in _deployment_targets(world)

    run_advised(world, remediation)
    cleared = _out(world.run(install, scenario_id="openclaw-enabled"))
    assert "requires an experimental flag" not in cleared
    assert "openclaw" in _deployment_targets(world)


# --- set-cowork-skills-dir / install-cowork-global -------------------------


def test_set_cowork_skills_dir(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _cowork_world(tmp_path, apm_binary_path, "cowork-dir")
    install = (*_INSTALL_ARGS, "--target", "copilot-cowork", "--global")
    reported = _run_failing(world, install, "cowork-dir-unknown")
    assert "Cowork has no auto-detection on Linux" in reported or "no OneDrive path" in reported
    remediation = advised("set-cowork-skills-dir", reported)

    skills_dir = world.scenario.isolated.home / "cowork-skills"
    skills_dir.mkdir()
    run_advised(world, remediation, substitutions={"<path>": str(skills_dir)})
    cleared = _out(world.run(install, scenario_id="cowork-dir-known"))
    assert "no auto-detection" not in cleared
    assert (skills_dir / "cowork-dir-cowork" / "SKILL.md").is_file()


def test_install_cowork_global(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _cowork_world(tmp_path, apm_binary_path, "cowork-scope")
    skills_dir = world.scenario.isolated.home / "cowork-skills"
    skills_dir.mkdir()
    world.run(
        ("config", "set", "copilot-cowork-skills-dir", str(skills_dir)),
        scenario_id="cowork-scope-dir",
    )
    reported = _run_failing(
        world, (*_INSTALL_ARGS, "--target", "copilot-cowork"), "cowork-project-scope"
    )
    assert "requires --global (user scope)" in reported
    remediation = advised("install-cowork-global", reported)

    run_advised(world, remediation)
    assert (skills_dir / "cowork-scope-cowork" / "SKILL.md").is_file()


# --- compile-root-context / compile-user-root-context ----------------------


def test_compile_root_context(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, targets=("codex",), install=False)
    reported = _out(world.install(scenario_id="codex-instructions"))
    assert "Instructions installed for codex" in reported
    remediation = advised("compile-root-context", reported, outputs="AGENTS.md")
    agents_md = world.root / "AGENTS.md"
    assert not agents_md.exists()

    run_advised(world, remediation)
    assert "# proof" in agents_md.read_text(encoding="utf-8")


def test_compile_user_root_context(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _new_scenario(tmp_path / "user-root", apm_binary_path)
    source = _publish(
        scenario,
        "user-root-kit",
        instruction="user-root",
        instruction_content="---\ndescription: user root proof\n---\n# user-root-proof\n",
    )
    _user_manifest(scenario, source.dependency)
    consumer = scenario.consumers.create("user-root-consumer")
    world = ProofWorld(scenario, source, consumer, dict(source.environment))
    reported = _out(
        world.run((*_INSTALL_ARGS, "--global", "--target", "codex"), scenario_id="user-install")
    )
    assert "Applicable instructions are available" in reported
    remediation = advised("compile-user-root-context", reported, targets="codex")
    agents_md = scenario.isolated.home / ".codex" / "AGENTS.md"
    assert not agents_md.exists()

    run_advised(world, remediation)
    assert "user-root-proof" in agents_md.read_text(encoding="utf-8")


# --- clean-download-cache / accept-new-content -----------------------------


def test_clean_download_cache(tmp_path: Path, apm_binary_path: Path) -> None:
    """A corrupted cached checkout no longer matches the locked hash."""
    world = installed_world(tmp_path, apm_binary_path)
    cache = world.scenario.isolated.cache_root
    cached = list(cache.glob("git/checkouts_v1/*/*/full/.apm/instructions/proof.instructions.md"))
    assert len(cached) == 1, sorted(cache.rglob("*"))
    cached[0].write_text("# tampered\n", encoding="ascii")
    shutil.rmtree(world.root / "apm_modules")

    reported = _run_failing(world, _INSTALL_ARGS, "cache-corrupted")
    assert "Content hash mismatch" in reported
    remediation = advised("clean-download-cache", reported)

    run_advised(world, remediation)
    world.install(scenario_id="cache-retry")  # then: retry
    assert "# tampered" not in (world.root / _INSTRUCTION).read_text(encoding="utf-8")
    assert world.audit_ci("cache-retry-audit")["passed"] is True


def test_accept_new_content(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    lock, row = _locked_row(world)
    good_hash = row["content_hash"]
    row["content_hash"] = "sha256:" + "0" * 64
    dump_yaml(lock, world.root / "apm.lock.yaml")
    shutil.rmtree(world.root / "apm_modules")

    reported = _run_failing(world, _INSTALL_ARGS, "lock-hash-differs")
    assert "Content hash mismatch" in reported
    lockfile = world.root / "apm.lock.yaml"
    remediation = advised(
        "accept-new-content", reported, global_flag="", package=_dep_key(world), lockfile=lockfile
    )

    lock, row = _locked_row(world)  # before: remove the package entry
    lock["dependencies"].remove(row)
    dump_yaml(lock, lockfile)
    run_advised(world, remediation)
    assert _locked_row(world)[1]["content_hash"] == good_hash


# --- clean-deps-cache ------------------------------------------------------


def test_clean_deps_cache(tmp_path: Path, apm_binary_path: Path) -> None:
    """A receiptless legacy plugin copy that lost its apm.yml is rejected."""
    scenario = _new_scenario(tmp_path / "deps-cache", apm_binary_path)
    source = _publish_legacy_plugin(scenario, "deps-cache-plugin", skill="legacy-skill")
    consumer = scenario.consumers.create(
        "deps-cache-consumer", dependencies=(source.dependency,), targets=("claude",)
    )
    world = ProofWorld(scenario, source, consumer, dict(source.environment))
    world.install(scenario_id="deps-cache-establish")
    cached = world.root / "apm_modules" / source.package.name
    # An APM 0.28 lock row hashes the receipt-less copy; the copy then loses apm.yml.
    (cached / ".apm" / ".plugin-skill-sources.json").unlink()
    lock = load_yaml(world.root / "apm.lock.yaml")
    lock["apm_version"] = "0.28.0"
    lock["dependencies"][0]["package_type"] = "marketplace_plugin"
    lock["dependencies"][0]["content_hash"] = compute_package_hash(cached)
    dump_yaml(lock, world.root / "apm.lock.yaml")
    (cached / "apm.yml").unlink()

    reported = _run_failing(world, _INSTALL_ARGS, "deps-cache-invalid")
    assert "Cached Claude Plugin" in reported, reported
    remediation = advised("clean-deps-cache", reported)

    run_advised(world, remediation)
    world.install(scenario_id="deps-cache-retry")  # then: retry
    assert (cached / "apm.yml").is_file()
    assert (world.root / ".claude" / "skills" / "legacy-skill" / "SKILL.md").is_file()


# --- reset-mcp-registry-url ------------------------------------------------


def test_reset_mcp_registry_url(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    config = world.scenario.isolated.config_root / "config.json"
    settings = json.loads(config.read_text("utf-8")) if config.exists() else {}
    settings["mcp_registry_url"] = "ftp://registry.invalid"
    config.write_text(json.dumps(settings), encoding="utf-8")
    add_mcp = ("install", "--no-policy", "--mcp", "proof-mcp", "--", "printf", "proof")

    reported = _run_failing(world, add_mcp, "mcp-registry-invalid")
    assert "Configured mcp-registry-url is invalid" in reported
    remediation = advised("reset-mcp-registry-url", reported)

    run_advised(world, remediation)
    world.run(add_mcp, scenario_id="mcp-registry-retry")  # then: retry
    assert "proof-mcp" in json.dumps(load_yaml(world.consumer.manifest_path))


# --- prune-package-copies (component) --------------------------------------


def _prune_world(tmp_path: Path, apm_binary_path: Path) -> tuple[ProofWorld, str, Path]:
    """A consumer of parent-kit, whose first commit depends on child-kit."""
    scenario = _new_scenario(tmp_path / "prune", apm_binary_path)
    child = _publish(scenario, "child-kit", instruction="child")
    parent = scenario.sources.create("parent-kit", dependencies=(child.dependency,))
    scenario.sources.add_instruction(parent, "parent", _instruction("parent"))
    repository = scenario.repositories.create("parent-kit", source_tree=parent.root)
    first = scenario.repositories.commit(repository, message="depend on child")
    manifest_path = repository.worktree / "apm.yml"
    manifest = load_yaml(manifest_path)
    del manifest["dependencies"]
    dump_yaml(manifest, manifest_path)
    second = scenario.repositories.commit(repository, message="drop child")
    remote_url = "https://github.com/apm-fixture-org/parent-kit"
    environment = scenario.repositories.url_rewrite_subprocess_env_many(
        ((repository, remote_url), (child.repository, child.remote_url))
    )
    source = _PublishedPackage(
        package=parent,
        repository=repository,
        commit=first,
        remote_url=remote_url,
        dependency={"git": remote_url, "ref": first.sha, "alias": "parent-kit"},
        environment=environment,
    )
    consumer = scenario.consumers.create(
        "prune-consumer", dependencies=(source.dependency,), targets=("copilot",)
    )
    world = ProofWorld(scenario, source, consumer, environment)
    world.install(scenario_id="prune-establish")
    child_copy = world.root / "apm_modules" / "child-kit"
    assert child_copy.is_dir()
    consumer_manifest = load_yaml(consumer.manifest_path)
    consumer_manifest["dependencies"]["apm"][0]["ref"] = second.sha
    dump_yaml(consumer_manifest, consumer.manifest_path)
    return world, remote_url, child_copy


def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the in-process CLI as hermetic as the binary's network guard."""

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError("IP network disabled by test environment")

    for name in ("getaddrinfo", "create_connection", "gethostbyname"):
        monkeypatch.setattr(socket, name, denied)


def test_prune_package_copies(
    tmp_path: Path, apm_binary_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, parent_url, child_copy = _prune_world(tmp_path, apm_binary_path)
    for name in list(os.environ):
        if name not in world.env:
            monkeypatch.delenv(name)
    for name, value in world.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(world.root)
    _deny_network(monkeypatch)
    monkeypatch.setattr(apm_config, "_config_cache", None)
    real_rmtree = path_security.safe_rmtree

    def locked_copy(path: Path, base_dir: Path) -> None:
        if Path(path) == child_copy:
            raise OSError(16, "Device or resource busy", str(path))
        real_rmtree(path, base_dir)

    monkeypatch.setattr(path_security, "safe_rmtree", locked_copy)
    result = CliRunner().invoke(
        cli, [*_INSTALL_ARGS, "--verbose", parent_url], catch_exceptions=False
    )
    reported = flat(result.output)
    assert result.exit_code == 0, result.output
    assert "Could not remove dropped dependency copy apm_modules/child-kit" in reported
    remediation = advised("prune-package-copies", reported)
    assert child_copy.is_dir()
    monkeypatch.setattr(path_security, "safe_rmtree", real_rmtree)

    run_advised(world, remediation)
    assert not child_copy.exists()
