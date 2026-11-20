"""Executable proofs for trust, policy and install-audit remediation advice (#4090).

Each proof induces the reported state through the installed CLI, reads the
advice the CLI printed, runs exactly the advised command through the same
binary, and observes the reported state gone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._mcp_chain_kit import Chain, stdio_server
from tests.integration.test_required_lifecycle_state_machine import (
    _hook,
    _new_scenario,
    _publish,
    _PublishedPackage,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.local_package import LocalPackage
from tests.utils.remediation_proof import (
    ProofWorld,
    advised,
    advised_argv,
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
    "trust-package",
    "trust-recommended",
    "trust-and-deploy",
    "trust-bin",
    "explain-policy",
    "deploy-approved-executables",
    "approve-transitive-mcp",
    "required-packages-absent",
    "configure-registry",
    "track-unmanaged-file",
    "policy-full-report",
    "force-install",
    "review-hidden-unicode",
    "readmit-codex-plugins",
    "complete-codex-migration",
    "verify-locked-content",
    "reinstall-user-package",
)

_HOOK = "echo proof-hook"
_PACKAGE = "apm-fixture-org/trust-kit"


def _output(result: CommandResult) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _hook_commands(root: Path) -> list[str]:
    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler.get("command", "")
        for group in data.get("hooks", {}).get("PreToolUse", [])
        for handler in group.get("hooks", [])
    ]


def _exec_status(root: Path) -> str | None:
    lock = LockFile.read(root / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


def _gated_world(tmp_path: Path, apm_binary_path: Path) -> ProofWorld:
    """A consumer whose executable gate is on, declaring one package that ships a hook."""
    world = installed_world(
        tmp_path,
        apm_binary_path,
        name="trust",
        targets=("claude",),
        hook_command=_HOOK,
        install=False,
    )
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["executables"] = {}
    dump_yaml(manifest, world.consumer.manifest_path)
    return world


def _pending(world: ProofWorld) -> str:
    return world.run(("approve", "--pending"), scenario_id="trust-pending").stdout


def _assert_parked(world: ProofWorld) -> None:
    assert _HOOK not in _hook_commands(world.root)
    assert _exec_status(world.root) == "gated_pending_approval"
    assert _PACKAGE in _pending(world)


def _assert_deployed(world: ProofWorld) -> None:
    assert _hook_commands(world.root) == [_HOOK]
    assert _exec_status(world.root) == "deployed"
    assert _PACKAGE not in _pending(world)


def _run_with_tty(world: ProofWorld, args: tuple[str, ...], answers: str) -> CommandResult:
    """Run the binary with a pseudo-terminal on stdin so the approval prompt fires."""
    import pty

    env = {
        key: value
        for key, value in world.env.items()
        if key not in ("CI", "APM_NON_INTERACTIVE", "GITHUB_ACTIONS")
    }
    command = (*world.scenario.runner._command, *args)
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            cwd=world.root,
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(slave)
    try:
        os.write(master, answers.encode("ascii"))
        stdout, stderr = process.communicate(timeout=120)
    finally:
        os.close(master)
    return CommandResult(
        command=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        cwd=world.root,
    )


def test_trust_package(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _gated_world(tmp_path, apm_binary_path)
    parked = world.install(scenario_id="trust-parked")
    _assert_parked(world)
    remediation = advised("trust-package", _output(parked), package=_PACKAGE)

    run_advised(world, remediation)

    assert _PACKAGE not in _pending(world)
    world.install(scenario_id="trust-deploy")
    _assert_deployed(world)


def test_explain_policy(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _gated_world(tmp_path, apm_binary_path)
    parked = world.install(scenario_id="explain-parked")
    _assert_parked(world)
    remediation = advised("explain-policy", _output(parked), package=_PACKAGE)

    explained = flat(run_advised(world, remediation).stdout)

    assert f"{_PACKAGE}#0.1.0: 1 hook(s)" in explained
    assert "verdict: blocked" in explained
    assert "hooks [x] blocked (layer: default-deny)" in explained


@pytest.mark.skipif(os.name == "nt", reason="pty.openpty is POSIX-only")
def test_trust_and_deploy(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _gated_world(tmp_path, apm_binary_path)
    declined = _run_with_tty(world, ("install", "--no-policy"), "n\n")
    assert declined.returncode == 0, _output(declined)
    _assert_parked(world)
    remediation = advised("trust-and-deploy", _output(declined), package=_PACKAGE)
    assert remediation.sentence() in flat(_output(declined))

    run_advised(world, remediation)
    world.install(scenario_id="trust-and-deploy-then")

    _assert_deployed(world)


@pytest.mark.skipif(os.name == "nt", reason="pty.openpty is POSIX-only")
def test_deploy_approved_executables(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _gated_world(tmp_path, apm_binary_path)
    approved = _run_with_tty(world, ("install", "--no-policy"), "y\n")
    assert approved.returncode == 0, _output(approved)
    assert _HOOK not in _hook_commands(world.root)
    assert _exec_status(world.root) == "gated_pending_approval"
    remediation = advised("deploy-approved-executables", _output(approved))

    run_advised(world, remediation)

    _assert_deployed(world)


def _plugin_world(
    tmp_path: Path, apm_binary_path: Path, name: str, files: dict[str, str]
) -> ProofWorld:
    """Publish a Claude plugin (no apm.yml) and declare it in a Claude consumer."""
    scenario = _new_scenario(tmp_path / name, apm_binary_path)
    source_root = scenario.isolated.package_root / name
    for relative, content in files.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")
        if relative.startswith("bin/"):
            path.chmod(0o755)
    repository = scenario.repositories.create(name, source_tree=source_root)
    commit = scenario.repositories.commit(repository, message=f"publish {name}")
    remote_url = f"https://github.com/apm-fixture-org/{name}"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    source = _PublishedPackage(
        package=LocalPackage(name=name, root=source_root, manifest_path=source_root / "plugin"),
        repository=repository,
        commit=commit,
        remote_url=remote_url,
        dependency={"git": remote_url, "ref": commit.sha},
        environment=environment,
    )
    consumer = scenario.consumers.create(
        f"{name}-consumer", dependencies=(source.dependency,), targets=("claude",)
    )
    (scenario.isolated.home / ".claude").mkdir(parents=True, exist_ok=True)
    return ProofWorld(scenario, source, consumer, dict(environment))


def test_trust_bin(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _plugin_world(
        tmp_path,
        apm_binary_path,
        "bin-plugin",
        {
            ".claude-plugin/plugin.json": '{"name": "bin-tool"}',
            "bin/tool": "#!/bin/sh\necho TOOL\n",
            "skills/hello/SKILL.md": "---\nname: hello\ndescription: bin fixture\n---\n# hello\n",
        },
    )
    home = world.scenario.isolated.home
    installed = world.install(scenario_id="trust-bin-skipped")
    assert "bin/ executables skipped (not trusted)" in flat(_output(installed))
    assert not list(home.rglob("bin/tool"))
    remediation = advised("trust-bin", _output(installed), package="apm-fixture-org/bin-plugin")

    run_advised(world, remediation)

    deployed = home / ".agents" / "plugins" / "bin-tool" / "bin" / "tool"
    assert deployed.read_text(encoding="ascii") == "#!/bin/sh\necho TOOL\n"


def _write_yaml(data: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(data, path)


def _chain_advised(chain: Chain, remediation, substitutions=None) -> CommandResult:
    """Run exactly the advised command in an MCP chain project."""
    return chain.ok(*advised_argv(remediation, substitutions))


def _seed_policy(chain: Chain, policy: dict[str, object]) -> None:
    chain.seed_org_policy(yaml.safe_dump({"name": "org", "version": "1.0", **policy}))
    chain.env.pop("APM_POLICY_DISABLE", None)


def test_trust_recommended(tmp_path: Path, apm_binary_path: Path) -> None:
    # The trust ladder admits a recommend entry that names the canonical identity
    # without approval; one naming the package by its manifest name leaves it
    # parked, and 'apm approve --recommended' trusts it.
    chain = Chain(tmp_path / "recommended", apm_binary_path, dirs=(".claude",))
    hooky = chain.project / "pkgs" / "hooky"
    _write_yaml({"name": "hooky", "version": "1.0.0"}, hooky / "apm.yml")
    hooks = hooky / ".apm" / "hooks" / "pretool.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps(_hook(_HOOK)), encoding="ascii")
    chain.write_manifest([], apm=["./pkgs/hooky"], targets=["claude"], executables={})
    _seed_policy(chain, {"executables": {"recommend": ["hooky"]}})

    parked = chain.ok("install")
    assert _HOOK not in _hook_commands(chain.project)
    assert "parked pending approval" in flat(_output(parked))
    remediation = advised("trust-recommended", _output(parked))

    approved = _chain_advised(chain, remediation)

    assert "Approved" in flat(_output(approved))
    assert "All packages with executables are approved." in _output(
        chain.ok("approve", "--pending")
    )
    chain.ok("install")
    assert _hook_commands(chain.project) == [_HOOK]


def test_approve_transitive_mcp(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = Chain(tmp_path / "transitive", apm_binary_path, dirs=(".claude",))
    _write_yaml(
        {"name": "parent", "version": "1.0.0", "dependencies": {"apm": ["../leaf"]}},
        chain.project / "pkgs" / "parent" / "apm.yml",
    )
    _write_yaml(
        {
            "name": "leaf",
            "version": "1.0.0",
            "dependencies": {"mcp": [stdio_server("leaf-server", "-y", "leaf@1")]},
        },
        chain.project / "pkgs" / "leaf" / "apm.yml",
    )
    chain.write_manifest([], apm=["./pkgs/parent"], targets=["claude"])
    one_off = chain.ok("install", "--trust-transitive-mcp")
    assert "leaf-server" in chain.servers("claude")
    remediation = advised("approve-transitive-mcp", _output(one_off), package="local:pkgs/leaf")

    _chain_advised(chain, remediation)

    chain.ok("install")
    assert "leaf-server" in chain.servers("claude")
    chain.ok("install")
    assert "leaf-server" in chain.servers("claude")


def test_policy_full_report(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = Chain(tmp_path / "full-report", apm_binary_path, dirs=(".claude",))
    chain.write_manifest([], apm=[], targets=["claude"])
    _seed_policy(
        chain,
        {
            "enforcement": "warn",
            "dependencies": {"require": ["acme/required"]},
            "registry_source": {"require": ["corp"]},
            "mcp": {"allow": ["corp-*"], "transport": {"allow": ["http"]}, "self_defined": "deny"},
            "compilation": {"target": {"allow": ["cursor"]}},
        },
    )
    arguments = ("--mcp", "proof-mcp", "--dry-run", "--", "printf", "hi")
    preview = flat(_output(chain.ok("install", *arguments)))
    assert preview.count("Policy warning:") == 5
    assert "... and 1 more policy warnings." in preview
    assert "compilation-target" not in preview
    remediation = advised("policy-full-report", preview)

    argv = advised_argv(remediation, {"<same-arguments>": "ARGS"})
    marker = argv.index("ARGS")
    report = flat(_output(chain.ok(*argv[:marker], *arguments, *argv[marker + 1 :])))

    assert report.count("Policy warning:") == 6
    assert "Policy warning: compilation-target" in report
    assert "more policy warnings" not in report
    assert not (chain.project / ".mcp.json").exists()


def _policy_audit(world: ProofWorld, policy: dict[str, object], scenario_id: str) -> dict:
    """Run 'apm audit --ci' against a local org policy file; return checks by name."""
    path = world.scenario.isolated.root / f"{scenario_id}-policy.yml"
    dump_yaml({"name": "org", "version": "1.0.0", "enforcement": "block", **policy}, path)
    result = world.run(
        ("audit", "--ci", "--no-fail-fast", "--policy", str(path), "--format", "json"),
        scenario_id=scenario_id,
        expect=None,
    )
    assert result.returncode in (0, 1), _output(result)
    return {item["name"]: item for item in json.loads(result.stdout)["checks"]}


def _check_text(check: dict) -> str:
    return flat(" ".join([check["message"], *(check.get("details") or [])]))


def test_required_packages_absent(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="required")
    extra = _publish(world.scenario, "extra-kit", instruction="extra")
    world.env = world.scenario.repositories.url_rewrite_subprocess_env_many(
        (
            (world.source.repository, world.source.remote_url),
            (extra.repository, extra.remote_url),
        )
    )
    manifest = load_yaml(world.consumer.manifest_path)
    manifest["dependencies"]["apm"].append(extra.dependency)
    dump_yaml(manifest, world.consumer.manifest_path)
    policy = {"dependencies": {"require": ["apm-fixture-org/extra-kit"]}}

    reported = _policy_audit(world, policy, "required-absent")["required-packages-deployed"]
    assert reported["passed"] is False, reported
    remediation = advised("required-packages-absent", _check_text(reported))
    assert remediation.then == "reinstall normally"

    run_advised(world, remediation)

    cleared = _policy_audit(world, policy, "required-cleared")["required-packages-deployed"]
    assert cleared["passed"] is True, cleared
    assert (world.root / ".github" / "instructions" / "extra.instructions.md").is_file()


def test_configure_registry(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="registry")
    policy = {"registry_source": {"require": ["corp"]}}

    reported = _policy_audit(world, policy, "registry-missing")["registry-source"]
    assert reported["passed"] is False, reported
    remediation = advised("configure-registry", _check_text(reported), name="corp")
    assert remediation.before == "run 'apm experimental enable registries'"
    assert f"{remediation.before}, then run" in _check_text(reported)

    world.run(("experimental", "enable", "registries"), scenario_id="registry-before")
    run_advised(world, remediation, substitutions={"<url>": "https://registry.example.test/apm"})

    cleared = _policy_audit(world, policy, "registry-configured")["registry-source"]
    assert cleared["passed"] is True, cleared


def test_track_unmanaged_file(tmp_path: Path, apm_binary_path: Path) -> None:
    world = installed_world(tmp_path, apm_binary_path, name="unmanaged")
    deployed = world.root / ".github" / "instructions" / "proof.instructions.md"
    content = deployed.read_bytes()
    world.run(("uninstall", "apm-fixture-org/unmanaged-kit"), scenario_id="unmanaged-drop")
    assert not deployed.exists()
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(content)
    policy = {"unmanaged_files": {"action": "deny"}}

    reported = _policy_audit(world, policy, "unmanaged-flagged")["unmanaged-files"]
    assert reported["passed"] is False, reported
    assert ".github/instructions/proof.instructions.md" in _check_text(reported)
    remediation = advised("track-unmanaged-file", _check_text(reported))

    run_advised(world, remediation, substitutions={"<ref>": world.source.remote_url})

    cleared = _policy_audit(world, policy, "unmanaged-tracked")["unmanaged-files"]
    assert cleared["passed"] is True, cleared
    assert deployed.read_bytes() == content


_HIDDEN = Path(".agents") / "skills" / "reviewer" / "SKILL.md"


def _hidden_unicode_world(tmp_path: Path, apm_binary_path: Path, mode: str) -> ProofWorld:
    """Install-time audit in *mode* over a kept, user-edited deployment hiding U+202E.

    The package source is clean, so only the install-time audit of deployed
    files sees the finding: dropping a target leaves the edited file in place.
    """
    world = installed_world(
        tmp_path / mode,
        apm_binary_path,
        name="unicode",
        targets=("claude", "copilot"),
        skill="reviewer",
        instruction=None,
    )
    world.scenario.consumers.set_targets(world.consumer, ("claude",))
    edited = world.root / _HIDDEN
    edited.write_text(f"{edited.read_text(encoding='utf-8')}\nedited \u202e\n", encoding="utf-8")
    world.run(("experimental", "enable", "external-scanners"), scenario_id="unicode-flag")
    world.run(("config", "set", "audit-on-install", mode), scenario_id="unicode-mode")
    return world


def test_force_install(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _hidden_unicode_world(tmp_path, apm_binary_path, "block")
    blocked = world.run(("install", "--no-policy"), scenario_id="unicode-blocked", expect=1)
    assert "Install-time audit blocked: 1 critical" in flat(_output(blocked))
    assert "\u202e" in (world.root / _HIDDEN).read_text(encoding="utf-8")
    remediation = advised("force-install", _output(blocked))

    forced = run_advised(world, remediation)

    assert "Deployed despite critical findings (--force)" in flat(_output(forced))


def test_review_hidden_unicode(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _hidden_unicode_world(tmp_path, apm_binary_path, "warn")
    warned = world.run(("install", "--no-policy"), scenario_id="unicode-warned", expect=None)
    assert "1 critical security finding(s)" in flat(_output(warned))
    remediation = advised("review-hidden-unicode", _output(warned))

    reviewed = run_advised(world, remediation, env={"COLUMNS": "200"}, expect=None)
    review = flat(_output(reviewed))

    assert _HIDDEN.as_posix() in review
    assert "U+202E" in review


def test_verify_locked_content(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _new_scenario(tmp_path / "framing", apm_binary_path)
    first = _publish(scenario, "first-kit", skill="first")
    second = _publish(scenario, "second-kit", skill="second")
    environment = scenario.repositories.url_rewrite_subprocess_env_many(
        ((first.repository, first.remote_url), (second.repository, second.remote_url))
    )
    consumer = scenario.consumers.create(
        "framing-consumer",
        dependencies=(first.dependency, second.dependency),
        targets=("claude",),
    )
    world = ProofWorld(scenario, first, consumer, environment)
    world.install(scenario_id="framing-seed")
    lock_path = world.root / "apm.lock.yaml"
    caches = {name: world.root / "apm_modules" / name for name in ("first-kit", "second-kit")}
    document = load_yaml(lock_path)
    document["lockfile_version"] = "1"
    for row in document["dependencies"]:
        row["content_hash"] = compute_package_hash(caches[row["alias"]], framed=False)
    dump_yaml(document, lock_path)
    next(caches["second-kit"].rglob("SKILL.md")).write_text("edited\n", encoding="utf-8")

    partial = world.install("--verbose", first.remote_url, scenario_id="framing-partial")
    assert load_yaml(lock_path)["lockfile_version"] == "1"
    package = "apm-fixture-org/second-kit"
    remediation = advised("verify-locked-content", _output(partial), package=package)
    assert f"{remediation.before}, then run" in flat(_output(partial))

    world.install(second.remote_url, scenario_id="framing-reinstall")
    run_advised(world, remediation)

    document = load_yaml(lock_path)
    assert document["lockfile_version"] == "4"
    rows = {row["alias"]: row for row in document["dependencies"]}
    assert rows["second-kit"]["content_hash"] == compute_package_hash(caches["second-kit"])


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="ascii")


def test_reinstall_user_package(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _new_scenario(tmp_path / "native-switch", apm_binary_path)
    isolated = scenario.isolated
    hooks = json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": _HOOK}]}]}}
    )
    source_root = isolated.package_root / "hooks-kit"
    _write_tree(
        source_root,
        {"apm.yml": "name: hooks-kit\nversion: 1.0.0\n", ".apm/hooks/session.json": hooks},
    )
    repository = scenario.repositories.create("hooks-kit", source_tree=source_root)
    loose = scenario.repositories.commit(repository, message="APM package with hooks")
    (repository.worktree / "apm.yml").unlink()
    (repository.worktree / ".apm" / "hooks" / "session.json").unlink()
    _write_tree(
        repository.worktree,
        {
            ".claude-plugin/plugin.json": '{"name": "hooks-kit", "version": "2.0.0"}',
            "hooks/hooks.json": hooks,
        },
    )
    native = scenario.repositories.commit(repository, message="Claude plugin with hooks")
    remote_url = "https://github.com/apm-fixture-org/hooks-kit"
    environment = scenario.repositories.url_rewrite_subprocess_env(repository, remote_url)
    claude_home = isolated.home / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    environment["CLAUDE_CONFIG_DIR"] = str(claude_home)
    user_manifest = isolated.config_root / "apm.yml"
    source = _PublishedPackage(
        package=LocalPackage(name="hooks-kit", root=source_root, manifest_path=user_manifest),
        repository=repository,
        commit=native,
        remote_url=remote_url,
        dependency={"git": remote_url, "ref": native.sha},
        environment=environment,
    )
    consumer = LocalPackage(name="user", root=isolated.work_root, manifest_path=user_manifest)
    world = ProofWorld(scenario, source, consumer, environment)

    def declare(ref: str) -> None:
        dump_yaml(
            {
                "name": "user",
                "version": "1.0.0",
                "targets": ["claude"],
                "dependencies": {"apm": [{"git": remote_url, "ref": ref}]},
            },
            user_manifest,
        )

    declare(loose.sha)
    world.run(("install", "--global", "--no-policy"), scenario_id="native-switch-loose")
    settings = claude_home / "settings.json"
    assert _HOOK in settings.read_text(encoding="utf-8")
    declare(native.sha)
    refused = world.run(
        ("install", "--global", "--no-policy"), scenario_id="native-switch-refused", expect=1
    )
    assert "Cannot switch apm-fixture-org/hooks-kit to native Claude" in flat(_output(refused))
    package = "apm-fixture-org/hooks-kit"
    remediation = advised("reinstall-user-package", _output(refused), package=package)
    assert remediation.before == f"run 'apm uninstall --global {package}'"

    world.run(("uninstall", "--global", package), scenario_id="native-switch-before")
    run_advised(world, remediation)

    declared = json.loads(settings.read_text(encoding="utf-8"))
    assert "hooks" not in declared
    assert [key.split("@")[0] for key, on in declared["enabledPlugins"].items() if on] == [
        "hooks-kit"
    ]
    assert (isolated.home / ".agents" / "plugins" / "hooks-kit" / "hooks" / "hooks.json").is_file()


# Completing a Codex-native install registers the bundle with a real Codex CLI
# app-server; the hermetic suite has none, so these proofs run the CLI from
# source in a fresh interpreter with registration (and, for the admission
# guard, the saved-state view) replaced. A fresh interpreter, not CliRunner,
# because apm_cli resolves its config paths at import.
_CODEX_SHIM = """
import sys
import apm_cli.install.phases.codex_plugins as phase
phase.synchronize_codex_plugins = lambda *args, **kwargs: []
if {diverge}:
    phase.candidates_from_lockfile = lambda *args, **kwargs: []
from apm_cli.entrypoint import main
sys.argv = ["apm", *sys.argv[1:]]
sys.exit(main())
"""

COMPONENT_PROOFS = {
    "readmit-codex-plugins": (
        "admission diverging from the saved lockfile is an internal consistency guard, and "
        "re-admission registers with a real Codex CLI app-server"
    ),
    "complete-codex-migration": (
        "the refusal is induced through the binary, but completing the migration registers "
        "with a real Codex CLI app-server"
    ),
}


def _codex_world(tmp_path: Path, apm_binary_path: Path, files: dict[str, str]) -> ProofWorld:
    """A user manifest targeting Codex that declares one local source directory."""
    scenario = _new_scenario(tmp_path / "codex", apm_binary_path)
    isolated = scenario.isolated
    source_root = isolated.package_root / "source"
    _write_tree(source_root, files)
    codex_home = isolated.home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    environment = {**scenario.environment, "CODEX_HOME": str(codex_home)}
    user_manifest = isolated.config_root / "apm.yml"
    dump_yaml(
        {
            "name": "user",
            "version": "1.0.0",
            "targets": ["codex"],
            "dependencies": {"apm": [str(source_root)]},
        },
        user_manifest,
    )
    consumer = LocalPackage(name="user", root=isolated.work_root, manifest_path=user_manifest)
    source = LocalPackage(name="source", root=source_root, manifest_path=source_root)
    return ProofWorld(scenario, source, consumer, environment)  # type: ignore[arg-type]


def _codex_component(world: ProofWorld, args: tuple[str, ...], *, diverge: bool) -> CommandResult:
    runner = ApmLifecycleRunner((sys.executable, "-c", _CODEX_SHIM.format(diverge=diverge)))
    return runner.run(args, scenario_id="codex-component", cwd=world.root, env=world.env)


_CODEX_SKILL = "---\nname: example\ndescription: Codex fixture\n---\n# example\n"
_CODEX_MANIFEST = json.dumps({"name": "example", "version": "1.0.0", "skills": "./skills"})
_CODEX_INSTALL = ("install", "-g", "--target", "codex", "--no-policy")


def test_readmit_codex_plugins(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _codex_world(
        tmp_path,
        apm_binary_path,
        {
            ".codex-plugin/plugin.json": _CODEX_MANIFEST,
            "skills/example/SKILL.md": _CODEX_SKILL,
        },
    )
    diverged = _codex_component(world, _CODEX_INSTALL, diverge=True)
    assert diverged.returncode == 1, _output(diverged)
    assert "Codex admission no longer matches" in flat(_output(diverged))
    remediation = advised("readmit-codex-plugins", _output(diverged))
    assert f"{remediation.before[:1].upper()}{remediation.before[1:]}, then run" in flat(
        _output(diverged)
    )

    reviewed = world.run(
        ("approve", "--pending"),
        scenario_id="codex-review",
        cwd=world.consumer.manifest_path.parent,
    )
    assert "All packages with executables are approved." in _output(reviewed)
    readmitted = _codex_component(world, advised_argv(remediation), diverge=False)

    assert readmitted.returncode == 0, _output(readmitted)
    assert "Codex admission no longer matches" not in _output(readmitted)


def test_complete_codex_migration(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _codex_world(
        tmp_path,
        apm_binary_path,
        {"SKILL.md": _CODEX_SKILL, "skills/example/SKILL.md": _CODEX_SKILL},
    )
    world.run(_CODEX_INSTALL, scenario_id="codex-loose")
    loose = world.scenario.isolated.home / ".agents" / "skills" / "source" / "SKILL.md"
    loose.write_text("User-edited loose skill\n", encoding="utf-8")
    source_root = world.source.root
    _write_tree(source_root, {".codex-plugin/plugin.json": _CODEX_MANIFEST})
    (source_root / "SKILL.md").unlink()
    refused = world.run(_CODEX_INSTALL, scenario_id="codex-migration-refused", expect=1)
    assert "has retained loose deployment files" in flat(_output(refused))
    remediation = advised("complete-codex-migration", _output(refused))

    preserved = world.scenario.isolated.root / "preserved-SKILL.md"
    loose.replace(preserved)
    completed = _codex_component(world, advised_argv(remediation), diverge=False)

    assert completed.returncode == 0, _output(completed)
    assert not loose.parent.exists()
    assert preserved.read_text(encoding="utf-8") == "User-edited loose skill\n"
    assert load_yaml(world.consumer.manifest_path.parent / "apm.lock.yaml")["deployments"] == []
