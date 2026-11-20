"""Executable proofs for the install, bundle, help and audit surface advice (#4090).

Each proof induces the state one ``core/remediation.py`` entry claims to
clear, observes the installed CLI report it with that entry's advice, runs
exactly the advised command (after its ``before`` step), and observes the
state gone. Informational entries show the command succeeds and delivers
what its promise says.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _publish_revision,
    _result_evidence,
    _skill,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.remediation_proof import (
    ProofWorld,
    advised,
    advised_argv,
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
    "init-project",
    "bootstrap-project-manifest",
    "bootstrap-user-manifest",
    "install-from-registry",
    "repack-plugin-bundle",
    "unpack-legacy-bundle",
    "repack-with-integrity",
    "compile-staged-instructions",
    "diagnose-connectivity",
    "update-refs",
    "apply-skill-selection",
    "list-installed-packages",
    "audit-policy-in-ci",
)

_PACKAGE = "apm-fixture-org/proof-kit"
_INSTRUCTION = ".github/instructions/proof.instructions.md"
_NO_POLICY = "--no-policy"


def _output(result: CommandResult) -> str:
    return result.stdout + result.stderr


def _locked(project: Path) -> list[dict[str, object]]:
    lock = load_yaml(project / "apm.lock.yaml") or {}
    return list(lock.get("dependencies") or [])


def _run_with_input(
    binary: Path, world: ProofWorld, args: tuple[str, ...], *, cwd: Path, answers: str
) -> CommandResult:
    """Run the binary with piped answers for its prompts (accepting defaults)."""
    command = (str(binary), *args)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=world.env,
        input=answers,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return CommandResult(command, completed.returncode, completed.stdout, completed.stderr, cwd)


def _run_in_terminal(
    binary: Path,
    world: ProofWorld,
    args: tuple[str, ...],
    *,
    cwd: Path,
    prompt: bytes,
    answer: bytes,
) -> CommandResult:
    """Run the binary on a pseudo-terminal and answer one confirmation prompt."""
    import pty
    import select

    command = (str(binary), *args)
    primary, secondary = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=world.env,
            stdin=secondary,
            stdout=secondary,
            stderr=secondary,
            close_fds=True,
        )
    finally:
        os.close(secondary)
    output = b""
    answered = False
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([primary], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(primary, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                output += chunk
                if not answered and prompt in output:
                    os.write(primary, answer)
                    answered = True
            elif process.poll() is not None:
                break
        else:
            process.kill()
    finally:
        os.close(primary)
    returncode = process.wait(timeout=30)
    text = output.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return CommandResult(command, returncode, text, "", cwd)


# -- manifest bootstrap ------------------------------------------------------


def _no_manifest_advice(world: ProofWorld, project: Path, *global_flag: str) -> str:
    reported = world.run(
        (*_INSTALL_ARGS, *global_flag), scenario_id="proof-no-manifest", cwd=project, expect=1
    )
    assert "[x] No " in reported.stdout, _result_evidence(reported)
    return _output(reported)


def test_init_project(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    project = world.scenario.isolated.work_root / "fresh"
    project.mkdir()
    remediation = advised("init-project", _no_manifest_advice(world, project))

    created = _run_with_input(
        apm_binary_path, world, advised_argv(remediation), cwd=project, answers="\n" * 8
    )

    assert created.returncode == 0, _result_evidence(created)
    assert (project / "apm.yml").is_file()
    world.run(_INSTALL_ARGS, scenario_id="proof-install-after-init", cwd=project)


def test_bootstrap_project_manifest(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    # An existing agent project (it has a harness) that never adopted APM.
    project = world.scenario.isolated.work_root / "adopting"
    (project / ".claude").mkdir(parents=True)
    remediation = advised("bootstrap-project-manifest", _no_manifest_advice(world, project))

    run_advised(world, remediation, substitutions={"<org/repo>": _PACKAGE}, cwd=project)

    manifest = load_yaml(project / "apm.yml")
    assert manifest["dependencies"]["apm"] == [_PACKAGE]
    assert [row["repo_url"] for row in _locked(project)] == [_PACKAGE]
    world.run(_INSTALL_ARGS, scenario_id="proof-install-after-bootstrap", cwd=project)


def test_bootstrap_user_manifest(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    user_root = world.scenario.isolated.config_root
    assert not (user_root / "apm.yml").exists()
    remediation = advised("bootstrap-user-manifest", _no_manifest_advice(world, world.root, "-g"))

    run_advised(world, remediation, substitutions={"<org/repo>": _PACKAGE})

    assert load_yaml(user_root / "apm.yml")["dependencies"]["apm"] == [_PACKAGE]
    assert [row["repo_url"] for row in _locked(user_root)] == [_PACKAGE]
    world.run((*_INSTALL_ARGS, "-g"), scenario_id="proof-user-install-after-bootstrap")


# -- local bundles -----------------------------------------------------------


def _bundle_target(world: ProofWorld, name: str, target: str = "copilot") -> Path:
    return world.scenario.consumers.create(name, targets=(target,)).root


def _pack(world: ProofWorld, *args: str) -> None:
    world.run(("pack", *args), scenario_id="proof-pack")


def _packed_directory(world: ProofWorld) -> Path:
    (manifest,) = (world.root / "build").glob("*/plugin.json")
    return manifest.parent


def _install_bundle(world: ProofWorld, project: Path, bundle: Path, *, expect: int | None = 0):
    return world.run(
        ("install", _NO_POLICY, str(bundle)),
        scenario_id="proof-install-bundle",
        cwd=project,
        expect=expect,
    )


def _legacy_bundle_refusal(world: ProofWorld, project: Path) -> tuple[Path, str]:
    _pack(world, "--format", "apm", "--archive", "--archive-format", "tar.gz", "-o", "legacy")
    (bundle,) = (world.root / "legacy").glob("*.tar.gz")
    refused = _install_bundle(world, project, bundle, expect=2)
    assert "legacy format" in flat(refused.stderr), _result_evidence(refused)
    assert not (project / _INSTRUCTION).exists()
    return bundle, _output(refused)


def test_unpack_legacy_bundle(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    project = _bundle_target(world, "legacy-target")
    bundle, output = _legacy_bundle_refusal(world, project)
    remediation = advised("unpack-legacy-bundle", output)

    run_advised(world, remediation, substitutions={"<bundle>": str(bundle)}, cwd=project)

    assert (project / _INSTRUCTION).read_bytes() == (world.root / _INSTRUCTION).read_bytes()


def test_repack_plugin_bundle(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    project = _bundle_target(world, "legacy-target")
    _bundle, output = _legacy_bundle_refusal(world, project)
    remediation = advised("repack-plugin-bundle", output)
    assert remediation.before == "change to the bundle's source project"

    run_advised(world, remediation, cwd=world.root)

    (repacked,) = (world.root / "build").glob("*.zip")
    installed = _install_bundle(world, project, repacked)
    assert "Installed 1 file(s) from local bundle" in flat(installed.stdout)
    assert (project / _INSTRUCTION).read_bytes() == (world.root / _INSTRUCTION).read_bytes()


def test_install_from_registry(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    project = _bundle_target(world, "archive-target")
    # A repository source archive is a package, not a packed bundle.
    archive = world.scenario.isolated.work_root / "proof-kit-main.zip"
    source = world.source.repository.worktree
    with zipfile.ZipFile(archive, "w") as handle:
        for path in sorted(source.rglob("*")):
            if path.is_file() and ".git" not in path.relative_to(source).parts:
                handle.write(path, f"proof-kit-main/{path.relative_to(source).as_posix()}")
    refused = _install_bundle(world, project, archive, expect=2)
    assert "not a valid APM bundle archive" in flat(refused.stderr), _result_evidence(refused)
    remediation = advised("install-from-registry", _output(refused))

    run_advised(world, remediation, substitutions={"<org/package>": _PACKAGE}, cwd=project)

    assert [row["repo_url"] for row in _locked(project)] == [_PACKAGE]
    assert (project / _INSTRUCTION).is_file()


def test_repack_with_integrity(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    project = _bundle_target(world, "integrity-target")
    _pack(world)
    # A bundle produced by an older APM carries no embedded lockfile.
    old_bundle = world.scenario.isolated.work_root / "old-bundle"
    shutil.copytree(_packed_directory(world), old_bundle)
    (old_bundle / "apm.lock.yaml").unlink()
    reported = _install_bundle(world, project, old_bundle)
    assert "Bundle has no apm.lock.yaml" in flat(reported.stdout), _result_evidence(reported)
    remediation = advised("repack-with-integrity", _output(reported))
    assert remediation.before == "change to the bundle's source project"

    run_advised(world, remediation, cwd=world.root)

    repacked = _packed_directory(world)
    assert (repacked / "apm.lock.yaml").is_file()
    installed = _install_bundle(world, project, repacked)
    assert "no apm.lock.yaml" not in flat(_output(installed)), _result_evidence(installed)
    assert (project / _INSTRUCTION).is_file()


def test_compile_staged_instructions(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    project = _bundle_target(world, "codex-target", target="codex")
    _pack(world)
    reported = _install_bundle(world, project, _packed_directory(world))
    assert "staged 1 instruction(s) for compile" in flat(reported.stdout)
    assert not (project / "AGENTS.md").exists()
    remediation = advised("compile-staged-instructions", _output(reported))

    run_advised(world, remediation, cwd=project)

    assert "# proof" in (project / "AGENTS.md").read_text(encoding="utf-8")


# -- connectivity ------------------------------------------------------------


def test_diagnose_connectivity(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="connectivity", install=False)
    scenario = world.scenario
    # Doctor probes github.com/git/git; route it to a local repository.
    probe = scenario.sources.create("git")
    probe_repository = scenario.repositories.create("git", source_tree=probe.root)
    scenario.repositories.commit(probe_repository, message="seed probe")
    env = scenario.repositories.url_rewrite_subprocess_env(
        probe_repository, "https://github.com/git/git"
    )
    # The SSH server rejects the key, as for an unauthorised deploy key.
    rejecting_ssh = scenario.isolated.root / "rejecting_ssh.py"
    rejecting_ssh.write_text(
        "import sys\n"
        "sys.stderr.write('git@proof-host: Permission denied (publickey).\\n')\n"
        "sys.exit(255)\n",
        encoding="ascii",
    )
    env["GIT_SSH_COMMAND"] = f'"{sys.executable}" "{rejecting_ssh}"'
    env["GIT_ALLOW_PROTOCOL"] = "file:ssh"
    consumer = scenario.consumers.create(
        "auth-consumer",
        dependencies=({"git": "ssh://git@git.proof.test/acme/auth-kit.git", "ref": "main"},),
        targets=("copilot",),
    )
    world = ProofWorld(scenario, world.source, consumer, env)
    reported = world.run((*_INSTALL_ARGS, "--update"), scenario_id="proof-auth-failure", expect=1)
    assert "SSH authentication failed" in flat(reported.stdout), _result_evidence(reported)
    remediation = advised("diagnose-connectivity", _output(reported))

    diagnosed = run_advised(world, remediation)

    rows = {
        line.split("|")[1].strip(): line.split("|")[2].strip()
        for line in diagnosed.stdout.splitlines()
        if line.count("|") >= 3
    }
    assert rows["git"] == "[+]", diagnosed.stdout
    assert rows["network"] == "[+]", diagnosed.stdout
    assert rows["auth"] == "[i]", diagnosed.stdout
    assert "No token" in diagnosed.stdout


# -- install help advice -----------------------------------------------------


def _install_help(world: ProofWorld) -> str:
    return world.run(("install", "--help"), scenario_id="proof-install-help").stdout


@pytest.mark.skipif(sys.platform == "win32", reason="the update plan prompt needs a POSIX pty")
def test_update_refs(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False)
    tracked = {**world.source.dependency, "ref": "main"}
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"] = [tracked]
    dump_yaml(manifest, world.consumer.manifest_path)
    world.install(scenario_id="proof-install-tracking-main")
    released = _publish_revision(world.scenario, world.source, "2")
    world.install(scenario_id="proof-install-keeps-lock")
    assert [row["resolved_commit"] for row in _locked(world.root)] == [world.source.commit.sha]
    outdated = world.run(("outdated",), scenario_id="proof-outdated")
    assert "1 outdated dependency found" in flat(outdated.stdout), _result_evidence(outdated)
    remediation = advised("update-refs", _install_help(world))

    updated = _run_in_terminal(
        apm_binary_path,
        world,
        advised_argv(remediation),
        cwd=world.root,
        prompt=b"Apply these changes?",
        answer=b"y\n",
    )

    assert updated.returncode == 0, _result_evidence(updated)
    assert [row["resolved_commit"] for row in _locked(world.root)] == [released.sha]
    current = world.run(("outdated",), scenario_id="proof-outdated-after-update")
    assert "All dependencies are up-to-date" in flat(current.stdout), _result_evidence(current)


def test_apply_skill_selection(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, install=False, targets=("claude",))
    worktree = world.source.repository.worktree
    for name in ("alpha", "beta"):
        skill = worktree / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(_skill(name), encoding="ascii")
    commit = world.scenario.repositories.commit(world.source.repository, message="add skills")
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"] = [
        {**world.source.dependency, "ref": commit.sha, "skills": ["alpha", "beta"]}
    ]
    dump_yaml(manifest, world.consumer.manifest_path)
    world.install(scenario_id="proof-install-two-skills")
    dropped = world.root / ".claude/skills/beta/SKILL.md"
    assert dropped.is_file()
    remediation = advised("apply-skill-selection", _install_help(world))
    assert remediation.before == "edit the skills: list in apm.yml"

    manifest["dependencies"]["apm"][0]["skills"] = ["alpha"]
    dump_yaml(manifest, world.consumer.manifest_path)
    assert (
        check(world.audit_ci("proof-audit-edited"), "skill-subset-consistency")["passed"] is False
    )

    run_advised(world, remediation)

    assert not dropped.exists()
    assert (world.root / ".claude/skills/alpha/SKILL.md").is_file()
    assert check(world.audit_ci("proof-audit-applied"), "skill-subset-consistency")["passed"]


# -- audit ---------------------------------------------------------------------


def test_list_installed_packages(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    refused = world.run(("audit", "proof-kit"), scenario_id="proof-audit-unknown", expect=1)
    assert "is not installed" in flat(refused.stdout), _result_evidence(refused)
    remediation = advised("list-installed-packages", _output(refused))

    listed = run_advised(world, remediation)

    assert _PACKAGE in listed.stdout, _result_evidence(listed)
    world.run(("audit", _PACKAGE), scenario_id="proof-audit-listed")


def test_audit_policy_in_ci(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path)
    policy = world.scenario.isolated.work_root / "apm-policy.yml"
    dump_yaml(
        {
            "name": "proof-policy",
            "version": "1.0.0",
            "enforcement": "block",
            "dependencies": {"deny": ["apm-fixture-org/**"]},
        },
        policy,
    )
    skipped = world.run(("audit", "--policy", str(policy)), scenario_id="proof-audit-no-ci")
    assert "dependency-denylist" not in _output(skipped)
    remediation = advised("audit-policy-in-ci", _output(skipped))

    enforced = run_advised(world, remediation, substitutions={"<source>": str(policy)}, expect=1)

    assert "dependency-denylist" in enforced.stdout, _result_evidence(enforced)
    assert "1 dependency(ies) match deny list" in flat(enforced.stdout)
