"""Executable proofs for the remediations ``apm audit`` advises (#4090).

Each proof induces the state one catalog entry claims to clear, observes the
installed CLI report it together with that entry's advice (read from the
structured ``remediations`` of an ``apm audit --ci --format json`` check when
the check carries them), runs exactly the advised command through the same
binary, and observes the reported state gone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._mcp_chain_kit import Chain, stdio_server
from tests.integration.test_local_bundle_compile_only_targets_lifecycle import _Bundle
from tests.integration.test_mcp_portable_approval_lifecycle import (
    SERVER,
    _approve,
    _leaf,
    _transitive,
)
from tests.integration.test_required_lifecycle_state_machine import (
    _publish_revision,
    _result_evidence,
)
from tests.integration.test_trust_integrity_consistency_lifecycle import (
    _allow,
    _bundle_hook_world,
    _bundle_key,
    _hooks_deployed,
    _revoke,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.remediation_proof import (
    AUDIT_CI_JSON,
    ProofWorld,
    advised,
    advised_argv,
    advised_json,
    check,
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
    "ref-mismatch",
    "ref-mismatch-update",
    "skill-subset-mismatch",
    "mcp-config-mismatch",
    "review-parked-mcp",
    "repair-ownership",
    "restore-managed-paths",
    "restage-bundle",
    "rebuild-local-graph",
    "populate-install-cache",
    "retire-bundle-hooks",
    "keep-bundle-hooks",
    "audit-from-project-root",
    "invalid-deployment-owners",
    "strip-hidden-unicode",
    "record-replacement-sources",
    "restore-user-packages",
    "pin-ref-for-audit",
    "regenerate-content-hashes",
)

_INSTRUCTION = ".github/instructions/proof.instructions.md"
_REPLACE_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def _world_at(runner: ApmLifecycleRunner, root: Path, env: Mapping[str, str]) -> ProofWorld:
    """Drive an existing project (bundle, MCP chain) through the proof harness."""
    return ProofWorld(
        scenario=SimpleNamespace(runner=runner),
        source=None,
        consumer=SimpleNamespace(root=root),
        env=dict(env),
    )


def _audit(world: ProofWorld, scenario_id: str, *, cwd: Path) -> dict[str, object]:
    """``ProofWorld.audit_ci`` from another directory."""
    result = world.run(AUDIT_CI_JSON, scenario_id=scenario_id, cwd=cwd, expect=None)
    assert result.returncode in (0, 1), _result_evidence(result)
    return json.loads(result.stdout)


def _edit_yaml(path: Path, edit: Callable[[dict], None]) -> None:
    data = load_yaml(path)
    edit(data)
    dump_yaml(data, path)


def _edit_lock(world: ProofWorld, edit: Callable[[dict], None]) -> None:
    _edit_yaml(world.root / "apm.lock.yaml", edit)


def _edit_dependency(world: ProofWorld, edit: Callable[[dict], None]) -> None:
    _edit_yaml(world.root / "apm.yml", lambda manifest: edit(manifest["dependencies"]["apm"][0]))


# -- Induced states read from a failed audit check's structured remediations --


def _float_ref(world: ProofWorld) -> None:
    _edit_dependency(world, lambda dep: dep.update(ref="main"))


def _move_revision_pin(world: ProofWorld) -> None:
    moved = _publish_revision(world.scenario, world.source, "moved")
    _edit_dependency(world, lambda dep: dep.update(ref=moved.sha))


def _select_skill(world: ProofWorld) -> None:
    _edit_dependency(world, lambda dep: dep.update(skills=["alpha"]))


def _declare_mcp(world: ProofWorld) -> None:
    _edit_yaml(
        world.root / "apm.yml",
        lambda manifest: manifest["dependencies"].update(
            mcp=[stdio_server("proof-server", "-y", "proof@1")]
        ),
    )


def _drop_ownership(world: ProofWorld) -> None:
    _edit_lock(world, lambda lock: lock.update(deployments=[]))


def _symlink_deployed_file(world: ProofWorld) -> None:
    deployed = world.root / _INSTRUCTION
    shadow = world.root / "shadow.md"
    shadow.write_bytes(deployed.read_bytes())
    deployed.unlink()
    deployed.symlink_to(shadow)


def _add_ghost_owner(world: ProofWorld) -> None:
    def edit(lock: dict) -> None:
        for row in lock["deployments"]:
            row["owners"] = [*row["owners"], "apm-fixture-org/ghost-kit"]

    _edit_lock(world, edit)


def _hide_unicode(world: ProofWorld) -> None:
    deployed = world.root / _INSTRUCTION
    text = deployed.read_text(encoding="utf-8")
    deployed.write_text(text.replace("# proof", "# pr\u202eoof"), encoding="utf-8")


def _remove_unsafe_listed_paths(world: ProofWorld, reported: Mapping[str, object]) -> None:
    for detail in reported["details"]:
        kind, _, value = str(detail).partition(": ")
        path = world.root / value
        if kind != "unresolved":
            continue
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


@dataclass(frozen=True)
class _AuditProof:
    remediation_id: str
    check: str
    induce: Callable[[ProofWorld], None]
    before: Callable[[ProofWorld, Mapping[str, object]], None] | None = None
    prompts: bool = False


AUDIT_PROOFS: tuple[_AuditProof, ...] = (
    _AuditProof("ref-mismatch", "ref-consistency", _float_ref),
    _AuditProof("ref-mismatch-update", "ref-consistency", _move_revision_pin, prompts=True),
    _AuditProof("skill-subset-mismatch", "skill-subset-consistency", _select_skill),
    _AuditProof("mcp-config-mismatch", "config-consistency", _declare_mcp),
    _AuditProof("repair-ownership", "content-integrity", _drop_ownership),
    _AuditProof(
        "restore-managed-paths",
        "content-integrity",
        _symlink_deployed_file,
        before=_remove_unsafe_listed_paths,
    ),
    _AuditProof("invalid-deployment-owners", "deployment-ledger-owners", _add_ghost_owner),
    _AuditProof("strip-hidden-unicode", "content-integrity", _hide_unicode),
)


def _run_confirmed(
    apm_binary_path: Path, world: ProofWorld, argv: tuple[str, ...]
) -> CommandResult:
    """Run a command that asks for confirmation on a terminal stdin and answer yes.

    'apm update' refuses to prompt without a terminal; a user running the
    advice in a shell confirms the plan it prints.
    """
    import pty

    master, slave = pty.openpty()
    try:
        os.write(master, b"y\n")
        completed = subprocess.run(
            (str(apm_binary_path), *argv),
            cwd=world.root,
            env=world.env,
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        os.close(slave)
        os.close(master)
    result = CommandResult(
        command=tuple(completed.args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        cwd=world.root,
    )
    assert result.returncode == 0, _result_evidence(result)
    return result


@pytest.mark.parametrize("proof", AUDIT_PROOFS, ids=lambda proof: proof.remediation_id)
def test_audit_check_remediation_clears_the_reported_check(
    tmp_path: Path, apm_binary_path: Path, proof: _AuditProof
) -> None:
    world = installed_world(tmp_path, apm_binary_path, skill="alpha")
    assert world.audit_ci("proof-audit-clean")["passed"] is True

    proof.induce(world)
    reported = check(world.audit_ci("proof-audit-reported"), proof.check)
    assert reported["passed"] is False, reported
    remediation = advised_json(proof.remediation_id, reported)
    assert f"'{remediation.command}' to {remediation.fixes_promise}" in flat(reported["message"])

    assert bool(remediation.before) is (proof.before is not None), remediation
    if proof.before is not None:
        proof.before(world, reported)
    if proof.prompts:
        _run_confirmed(apm_binary_path, world, advised_argv(remediation))
    else:
        run_advised(world, remediation)
    cleared = world.audit_ci("proof-audit-cleared")
    assert check(cleared, proof.check)["passed"] is True, cleared
    assert cleared["passed"] is True, cleared


# -- Advice carried by a passing check or a replay failure ---------------------


def _checks_mentioning(payload: Mapping[str, object], text: str) -> list[dict[str, object]]:
    return [item for item in payload["checks"] if text in flat(str(item["message"]))]


def test_populate_install_cache(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    shutil.rmtree(world.root / "apm_modules" / "proof-kit")

    reported = check(world.audit_ci("proof-audit-reported"), "drift")
    assert str(reported["message"]).startswith("drift skipped"), reported
    remediation = advised_json("populate-install-cache", reported)

    run_advised(world, remediation)
    cleared = world.audit_ci("proof-audit-cleared")
    assert check(cleared, "drift")["message"] == "no drift detected against lockfile", cleared
    assert cleared["passed"] is True, cleared


def test_audit_from_project_root(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    subdirectory = world.root / "docs"
    subdirectory.mkdir()

    reported = check(_audit(world, "proof-audit-reported", cwd=subdirectory), "manifest-missing")
    assert reported["passed"] is False, reported
    remediation = advised_json("audit-from-project-root", reported)
    assert remediation.before and "project root" in remediation.before

    result = run_advised(world, remediation, cwd=world.root)
    assert "lockfile-exists" in result.stdout + result.stderr, _result_evidence(result)


def test_regenerate_content_hashes(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    policy = world.root.parent / "require-hashes-policy.yml"
    dump_yaml(
        {
            "name": "require-hashes",
            "version": "1.0.0",
            "enforcement": "block",
            "security": {"integrity": {"require_hashes": True}},
        },
        policy,
    )
    audit_args = ("audit", "--ci", "--no-fail-fast", "--format", "json", "--policy", str(policy))

    def audit(scenario_id: str) -> dict[str, object]:
        result = world.run(audit_args, scenario_id=scenario_id, expect=None)
        assert result.returncode in (0, 1), _result_evidence(result)
        return json.loads(result.stdout)

    assert check(audit("proof-audit-clean"), "dependency-content-hashes")["passed"] is True
    _edit_lock(world, lambda lock: lock["dependencies"][0].pop("content_hash"))

    reported = check(audit("proof-audit-reported"), "dependency-content-hashes")
    assert reported["passed"] is False, reported
    remediation = advised_json("regenerate-content-hashes", reported)

    run_advised(world, remediation)
    cleared = audit("proof-audit-cleared")
    assert check(cleared, "dependency-content-hashes")["passed"] is True, cleared
    assert cleared["passed"] is True, cleared


def test_record_replacement_sources(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)

    def replace(manifest: dict) -> None:
        manifest["$schema"] = _REPLACE_SCHEMA
        manifest["replace"] = [
            {
                "for": {"git": "https://github.com/unused-replacement-fixture/base.git"},
                "with": {"path": "../intentionally-absent"},
            }
        ]

    _edit_yaml(world.root / "apm.yml", replace)
    shutil.rmtree(world.root / "apm_modules")

    reported = _checks_mentioning(world.audit_ci("proof-audit-reported"), "Replacement rules")
    assert reported and all(item["passed"] is False for item in reported), reported
    remediation = advised("record-replacement-sources", str(reported[0]["message"]))

    run_advised(world, remediation)
    cleared = world.audit_ci("proof-audit-cleared")
    assert not _checks_mentioning(cleared, "Replacement rules"), cleared
    assert cleared["passed"] is True, cleared


def test_pin_ref_for_audit(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    _edit_dependency(world, lambda dep: dep.pop("ref"))
    world.install(scenario_id="proof-install-unpinned")
    _edit_lock(world, lambda lock: lock["dependencies"][0].pop("resolved_commit"))

    # 'apm export-patch' replays the locked install the way the audit drift check does.
    replay = ("export-patch", "--dry-run")
    refused = world.run(replay, scenario_id="proof-replay-reported", expect=1)
    assert "no resolved_commit" in flat(refused.stdout + refused.stderr), refused
    remediation = advised("pin-ref-for-audit", refused.stdout + refused.stderr)
    assert remediation.before and "pin the dependency" in remediation.before

    _edit_dependency(world, lambda dep: dep.update(ref=world.source.commit.sha))
    run_advised(world, remediation)
    locked = load_yaml(world.root / "apm.lock.yaml")["dependencies"][0]
    assert locked["resolved_commit"] == world.source.commit.sha, locked
    world.run(replay, scenario_id="proof-replay-cleared")
    assert world.audit_ci("proof-audit-cleared")["passed"] is True


def test_restore_user_packages(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    user_root = world.scenario.isolated.config_root
    user_root.mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {
            "name": "user-profile",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [world.source.dependency]},
        },
        user_root / "apm.yml",
    )
    world.run(
        ("install", "--global", "--no-policy"), scenario_id="proof-install-global", cwd=user_root
    )
    assert _audit(world, "proof-audit-clean", cwd=user_root)["passed"] is True
    shutil.rmtree(user_root / "apm_modules")

    reported = _checks_mentioning(
        _audit(world, "proof-audit-reported", cwd=user_root), "materialization is missing"
    )
    assert reported and all(item["passed"] is False for item in reported), reported
    remediation = advised("restore-user-packages", str(reported[0]["message"]))

    run_advised(world, remediation, cwd=user_root)
    cleared = _audit(world, "proof-audit-cleared", cwd=user_root)
    assert (user_root / "apm_modules").is_dir()
    assert cleared["passed"] is True, cleared


# -- Local bundles: staged compile input and hook trust ------------------------

_STAGED = "apm_modules/bundler/.apm/instructions/glob.instructions.md"


def test_restage_bundle(tmp_path: Path, apm_binary_path: Path) -> None:
    bundles = _Bundle(tmp_path, apm_binary_path)
    bundle = bundles.pack()
    project = bundles.consumer("codex")
    bundles.apm(project, "install", str(bundle), "--no-policy")
    world = _world_at(bundles.runner, project, bundles.env)
    assert world.audit_ci("proof-audit-clean")["passed"] is True
    staged = project / _STAGED
    staged.write_text(staged.read_text(encoding="ascii") + "Injected.\n", encoding="ascii")

    reported = check(world.audit_ci("proof-audit-reported"), "content-integrity")
    assert reported["passed"] is False, reported
    remediation = advised_json("restage-bundle", reported)

    run_advised(world, remediation, substitutions={"<bundle>": str(bundle)})
    assert "Injected." not in staged.read_text(encoding="ascii")
    cleared = world.audit_ci("proof-audit-cleared")
    assert cleared["passed"] is True, cleared


def test_retire_bundle_hooks(tmp_path: Path, apm_binary_path: Path) -> None:
    _scope, runner, env, _bundle, project = _bundle_hook_world(tmp_path, apm_binary_path)
    world = _world_at(runner, project, env)
    assert world.audit_ci("proof-audit-clean")["passed"] is True
    _revoke(project)

    reported = check(world.audit_ci("proof-audit-reported"), "local-bundle-hooks")
    assert reported["passed"] is False, reported
    remediation = advised_json("retire-bundle-hooks", reported)

    run_advised(world, remediation)
    assert not _hooks_deployed(project)
    cleared = world.audit_ci("proof-audit-cleared")
    assert check(cleared, "local-bundle-hooks")["passed"] is True, cleared
    assert cleared["passed"] is True, cleared


def test_keep_bundle_hooks(tmp_path: Path, apm_binary_path: Path) -> None:
    _scope, runner, env, bundle, project = _bundle_hook_world(tmp_path, apm_binary_path)
    world = _world_at(runner, project, env)
    _revoke(project)
    retired = world.run(("install", "--no-policy"), scenario_id="proof-retire")
    assert not _hooks_deployed(project)
    remediation = advised("keep-bundle-hooks", retired.stdout + retired.stderr)
    assert remediation.before and "approve the bundle" in remediation.before

    _allow(project, _bundle_key(bundle))
    run_advised(world, remediation, substitutions={"<bundle>": str(bundle)})
    assert _hooks_deployed(project)
    assert world.audit_ci("proof-audit-cleared")["passed"] is True


# -- MCP approval and local dependency graphs ----------------------------------


def test_review_parked_mcp(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = _transitive(tmp_path, apm_binary_path)
    chain.ok("install")
    _approve(chain)
    chain.ok("install")
    _leaf(chain, "-y", "leaf@2")
    chain.ok("install")
    world = _world_at(chain.runner, chain.project, chain.env)

    reported = check(world.audit_ci("proof-audit-reported"), "config-consistency")
    assert reported["passed"] is True and SERVER in str(reported["message"]), reported
    remediation = advised_json("review-parked-mcp", reported)

    # Informational: the listing names each parked server and why it is parked.
    review = flat(run_advised(world, remediation).stdout)
    assert SERVER in review and "changed since approval" in review, review
    assert "leaf@2" in review, review


def test_rebuild_local_graph(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = Chain(tmp_path / "graph", apm_binary_path, dirs=(".github",))
    for name, apm in (("parent", ["../leaf"]), ("leaf", [])):
        package = chain.project / "pkgs" / name
        instruction = package / ".apm" / "instructions" / f"{name}.instructions.md"
        instruction.parent.mkdir(parents=True)
        instruction.write_text(
            f"---\napplyTo: '**'\ndescription: {name}\n---\n# {name}\n", encoding="ascii"
        )
        (package / "apm.yml").write_text(
            yaml.safe_dump(
                {"name": name, "version": "1.0.0", "dependencies": {"apm": apm}},
            ),
            encoding="utf-8",
        )
    chain.write_manifest([], apm=["./pkgs/parent"], targets=["copilot"])
    chain.ok("install", "--no-policy")
    world = _world_at(chain.runner, chain.project, chain.env)
    assert world.audit_ci("proof-audit-clean")["passed"] is True

    def orphan(lock: dict) -> None:
        children = [dep for dep in lock["dependencies"] if dep.get("resolved_by")]
        assert children, lock
        for dep in children:
            dep["resolved_by"] = "_local/ghost"

    _edit_lock(world, orphan)

    reported = check(world.audit_ci("proof-audit-reported"), "drift")
    assert reported["passed"] is False, reported
    remediation = advised_json("rebuild-local-graph", reported)

    run_advised(world, remediation)
    cleared = world.audit_ci("proof-audit-cleared")
    assert check(cleared, "drift")["passed"] is True, cleared
    assert cleared["passed"] is True, cleared
