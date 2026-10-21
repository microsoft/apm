"""Installed-CLI consent for native Claude inline hooks, without invoking Claude."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_frontmatter, load_yaml, yaml_to_str
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_HOOKS = {
    "PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo inline-hook-witness"}]}
    ]
}


def _primitive(
    factory: LocalPackageFactory, package: LocalPackage, kind: str, name: str, *, hooks: bool
) -> Path:
    metadata: dict[str, object] = {"name": name, "description": "Harmless inline hook fixture"}
    if hooks:
        metadata["hooks"] = _HOOKS
    content = f"---\n{yaml_to_str(metadata)}---\nDo not execute commands.\n"
    return getattr(factory, f"add_{kind}")(package, name, content)


def _interactive_install(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    workspace: Path,
    env: dict[str, str],
    answer: bool,
) -> CommandResult:
    """Give the real APM process a real TTY, without invoking a native client."""
    import pty

    interactive_env = dict(env)
    interactive_env.pop("CI", None)
    interactive_env.pop("APM_NON_INTERACTIVE", None)
    master, slave = pty.openpty()
    try:
        os.write(master, b"y\n" if answer else b"n\n")
        return runner.run(
            args,
            cwd=workspace,
            env=interactive_env,
            stdin_fd=slave,
            scenario_id="inline-real-tty",
        )
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(os.name == "nt", reason="Real TTY fixture uses POSIX pty.openpty")
@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("answer", [False, True])
def test_inline_interactive_consent_uses_personal_grant_and_later_install(
    tmp_path: Path, apm_binary_path: Path, kind: str, user_scope: bool, answer: bool
) -> None:
    """A real consent prompt never activates hooks in the already-parked install."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    _primitive(factory, dependency, kind, "plain", hooks=False)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    identity = dependency.root.as_posix() if user_scope else "../guarded"
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"] = {"apm": [{"path": identity}]}
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, workspace / "apm.yml")
    manifest_before = (workspace / "apm.yml").read_bytes()
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    args = ("install", "--no-policy", *(("--global",) if user_scope else ()))
    result = _interactive_install(runner, args, workspace, isolated.subprocess_env(), answer)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Trust " in result.stdout
    assert "direct dependency" in result.stdout
    assert (workspace / "apm.yml").read_bytes() == manifest_before
    subdir, suffix = {
        "command": ("commands", ".md"),
        "skill": ("skills", "/SKILL.md"),
        "agent": ("agents", ".md"),
    }[kind]
    protected = deployment / ".claude" / subdir / f"protected{suffix}"
    assert not protected.exists()
    assert (deployment / ".claude" / subdir / f"plain{suffix}").is_file()
    config_path = isolated.config_root / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    assert (
        bool(
            config.get("executables", {}).get("allow", {}).get(f"{identity}#0.1.0", {}).get("hooks")
        )
        is answer
    )
    later = runner.run(args, cwd=workspace, env=isolated.subprocess_env())
    assert later.returncode == 0, later.stdout + later.stderr
    assert protected.exists() is answer


@pytest.mark.skipif(os.name == "nt", reason="Real TTY fixture uses POSIX pty.openpty")
@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("denying_layer", ["project", "user"])
def test_inline_hard_deny_is_not_offered_as_personal_approval(
    tmp_path: Path, apm_binary_path: Path, kind: str, denying_layer: str
) -> None:
    """A fixture answer cannot create a dormant grant beneath a higher-priority deny."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"allow": {}}
    deny = {"deny": {"../guarded": {"hooks": True}}}
    if denying_layer == "project":
        manifest["executables"].update(deny)
    else:
        (isolated.config_root / "config.json").write_text(
            json.dumps({"executables": deny}), encoding="utf-8"
        )
    dump_yaml(manifest, consumer.manifest_path)
    result = _interactive_install(
        ApmLifecycleRunner((str(apm_binary_path),)),
        ("install", "--no-policy"),
        consumer.root,
        isolated.subprocess_env(),
        True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Trust " not in result.stdout
    assert "denied" in result.stdout.lower()
    assert "pending approval" not in result.stdout.lower()
    config_path = isolated.config_root / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    assert config.get("executables", {}).get("allow", {}) == {}


@pytest.mark.skipif(os.name == "nt", reason="Real TTY fixture uses POSIX pty.openpty")
@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
def test_transitive_inline_prompt_and_approval_keep_resolver_identity(
    tmp_path: Path, apm_binary_path: Path, kind: str
) -> None:
    """Transitive provenance and grants use the actual resolved child, not its parent."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    factory.create("parent", dependencies=({"path": "../guarded"},))
    consumer = factory.create(
        "consumer", dependencies=({"path": "../parent"},), targets=("claude",)
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, consumer.manifest_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    pending = _interactive_install(
        runner, ("install", "--no-policy"), consumer.root, isolated.subprocess_env(), False
    )
    assert pending.returncode == 0, pending.stdout + pending.stderr
    assert "transitive via ../parent" in pending.stdout
    lock = LockFile.read(consumer.root / "apm.lock.yaml")
    assert lock is not None
    [child] = [dependency for dependency in lock.dependencies.values() if dependency.depth == 2]
    identity = child.get_unique_key()
    approved = runner.run(("approve", identity), cwd=consumer.root, env=isolated.subprocess_env())
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert load_yaml(consumer.manifest_path)["executables"]["allow"] == {
        f"{identity}#0.1.0": {"hooks": True}
    }
    installed = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    [deployed] = list((consumer.root / ".claude").rglob("*.md"))
    assert load_frontmatter(deployed).metadata["hooks"] == _HOOKS


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("user_scope", [False, True])
def test_inline_update_cold_policy_replay_and_lock_status_are_consistent(
    tmp_path: Path, apm_binary_path: Path, kind: str, user_scope: bool
) -> None:
    """Real update and cold audit retain current consent instead of trusting lock status."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    source = _primitive(factory, dependency, kind, "protected", hooks=True)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("guarded", source_tree=dependency.root)
    repositories.commit(repository, message="Initial harmless inline hook")
    source = repository.worktree / source.relative_to(dependency.root)
    identity = "fixture/inline-hooks"
    env = repositories.url_rewrite_subprocess_env(
        repository, "https://github.com/fixture/inline-hooks"
    )
    consumer = factory.create(
        "consumer", dependencies=({"git": identity, "ref": "main"},), targets=("claude",)
    )
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"allow": {identity: {"hooks": True}}}
    dump_yaml(manifest, workspace / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    install_args = ("install", "--no-policy", *(("--global",) if user_scope else ()))
    installed = runner.run(install_args, cwd=workspace, env=env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    subdir, suffix = {
        "command": ("commands", ".md"),
        "skill": ("skills", "/SKILL.md"),
        "agent": ("agents", ".md"),
    }[kind]
    deployed = deployment / ".claude" / subdir / f"protected{suffix}"
    metadata = load_frontmatter(source).metadata
    metadata["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "echo updated-inline-witness"
    source.write_text(f"---\n{yaml_to_str(metadata)}---\nDo not execute commands.\n")
    updated_commit = repositories.commit(repository, message="Update harmless inline hook")
    updated = runner.run(
        ("update", "--yes", *(("--global",) if user_scope else ())),
        cwd=workspace,
        env=env,
    )
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert load_frontmatter(deployed).metadata["hooks"] == metadata["hooks"]
    updated_lock = LockFile.read(workspace / "apm.lock.yaml")
    assert updated_lock is not None
    assert updated_lock.get_dependency(identity).resolved_commit == updated_commit.sha

    policy_path = isolated.root / "policy.yml"
    dump_yaml(
        {"name": "inline-deny", "enforcement": "block", "executables": {"deny_all": True}},
        policy_path,
    )
    modules = workspace / "apm_modules"
    saved_modules = isolated.temp_root / "saved-modules"

    def audit_readonly(*policy_args: str, expected: bool) -> None:
        def snapshot() -> dict[Path, bytes]:
            return {
                path: path.read_bytes()
                for path in isolated.root.rglob("*")
                if path.is_file() and not path.is_relative_to(isolated.temp_root)
            }

        before = snapshot()
        materialized_before = modules.exists()
        result = runner.run(
            ("audit", "--ci", "--format", "json", *policy_args),
            cwd=workspace,
            env=env,
        )
        assert snapshot() == before
        report = json.loads(result.stdout)
        assert report["passed"] is expected, result.stdout + result.stderr
        assert (result.returncode == 0) is expected, result.stdout + result.stderr
        assert modules.exists() is materialized_before
        if user_scope and not materialized_before:
            assert any(
                "installed package materialization is missing" in check["message"]
                for check in report["checks"]
                if not check["passed"]
            )

    audit_readonly("--no-policy", expected=True)
    audit_readonly("--policy", str(policy_path), expected=False)
    modules.rename(saved_modules)
    audit_readonly("--no-policy", expected=not user_scope)
    audit_readonly("--policy", str(policy_path), expected=False)
    saved_modules.rename(modules)
    denied = runner.run(("deny", identity), cwd=workspace, env=env)
    assert denied.returncode == 0, denied.stdout + denied.stderr
    revoked = runner.run(install_args, cwd=workspace, env=env)
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    assert not deployed.exists()
    lock_path = workspace / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    assert lock is not None
    assert [record.exec_status for record in lock.dependencies.values()] == ["denied"]
    for dependency_record in lock.dependencies.values():
        dependency_record.exec_status = "deployed"
    lock.write(lock_path)
    audit_readonly("--no-policy", expected=True)
    audit_readonly("--policy", str(policy_path), expected=True)
    modules.rename(saved_modules)
    audit_readonly("--no-policy", expected=not user_scope)
    audit_readonly("--policy", str(policy_path), expected=not user_scope)


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("versioned_deny", [False, True])
def test_denied_inline_hook_withholds_whole_primitive(
    tmp_path: Path, apm_binary_path: Path, kind: str, versioned_deny: bool
) -> None:
    """Denying hooks never installs a primitive with its protection removed."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    _primitive(factory, dependency, kind, "plain", hooks=False)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    manifest = load_yaml(consumer.manifest_path)
    deny_key = "../guarded#0.1.0" if versioned_deny else "../guarded"
    manifest["executables"] = {
        "allow": {"../guarded": {"hooks": True}},
        "deny": {deny_key: {"hooks": True}},
    }
    dump_yaml(manifest, consumer.manifest_path)
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
        scenario_id=f"inline-{kind}-denied",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    subdir, suffix = {
        "command": ("commands", ".md"),
        "skill": ("skills", "/SKILL.md"),
        "agent": ("agents", ".md"),
    }[kind]
    native = consumer.root / ".claude" / subdir
    assert (native / f"plain{suffix}").is_file(), result.stdout + result.stderr
    assert not (native / f"protected{suffix}").exists(), result.stdout + result.stderr
    assert "withheld" in result.stdout.lower(), result.stdout + result.stderr
    lock = LockFile.read(consumer.root / "apm.lock.yaml")
    assert lock is not None
    assert [record.exec_status for record in lock.dependencies.values()] == ["denied"]


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("resource", [False, True])
@pytest.mark.parametrize("user_scope", [False, True])
def test_approved_inline_hook_preserves_native_definition(
    tmp_path: Path, apm_binary_path: Path, kind: str, resource: bool, user_scope: bool
) -> None:
    """Approval retains native scoping and required bytes without invoking a client."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    source = _primitive(factory, dependency, kind, "protected", hooks=True)
    document = deepcopy(_HOOKS)
    handler = document["PreToolUse"][0]["hooks"][0]
    handler["timeout"] = 5
    if kind != "agent":
        handler["once"] = True
    if resource:
        script = source.parent / "scripts" / "guard.py"
        script.parent.mkdir()
        script.write_text("raise SystemExit(0)\n", encoding="utf-8")
        handler["command"] = "python ./scripts/guard.py"
    metadata = load_frontmatter(source).metadata
    metadata.update(
        {
            "hooks": document,
            "allowed-tools": "Read",
            "disable-model-invocation": True,
            "user-invocable": False,
        }
    )
    source.write_text(f"---\n{yaml_to_str(metadata)}---\nDo not execute commands.\n")
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    identity = dependency.root.as_posix() if user_scope else "../guarded"
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"] = {"apm": [{"path": identity}]}
    manifest["executables"] = {"allow": {identity: {"hooks": True}}}
    dump_yaml(manifest, workspace / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    for operation in ("install", "install"):
        result = runner.run(
            (operation, "--no-policy", *(("--global",) if user_scope else ())),
            cwd=workspace,
            env=isolated.subprocess_env(),
            scenario_id=f"inline-{kind}-approved-{resource}",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        subdir, suffix = {
            "command": ("commands", ".md"),
            "skill": ("skills", "/SKILL.md"),
            "agent": ("agents", ".md"),
        }[kind]
        native = deployment / ".claude" / subdir / f"protected{suffix}"
        deployed = load_frontmatter(native).metadata
        actual = deployed["hooks"]["PreToolUse"][0]
        assert actual["matcher"] == "Bash"
        assert actual["hooks"][0]["timeout"] == 5
        if kind != "agent":
            assert actual["hooks"][0]["once"] is True
        else:
            assert "once" not in actual["hooks"][0]
        assert deployed["allowed-tools"] == "Read"
        assert deployed["disable-model-invocation"] is True
        assert deployed["user-invocable"] is False
        assert not (deployment / ".claude" / "settings.json").exists()
        if resource:
            command = actual["hooks"][0]["command"]
            prefix = deployment.as_posix() if user_scope else "${CLAUDE_PROJECT_DIR}"
            assert f"{prefix}/.claude/hooks/inline-" in command
            resources = list((deployment / ".claude/hooks").rglob("guard.py"))
            assert len(resources) == 1
            assert resources[0].read_bytes() == script.read_bytes()
        else:
            assert actual["hooks"][0]["command"] == handler["command"]
    audit = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        cwd=workspace,
        env=isolated.subprocess_env(),
    )
    assert audit.returncode == 0, audit.stdout + audit.stderr


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("user_scope", [False, True])
def test_inline_hook_approval_revocation_and_readonly_audit(
    tmp_path: Path, apm_binary_path: Path, kind: str, user_scope: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    _primitive(factory, dependency, kind, "plain", hooks=False)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    identity = (
        dependency.root.as_posix()
        if user_scope
        else os.path.relpath(dependency.root, workspace).replace(os.sep, "/")
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"] = {"apm": [{"path": identity}]}
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, workspace / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    def run(*args: str):
        return runner.run(
            args, cwd=workspace, env=isolated.subprocess_env(), scenario_id=f"inline-{kind}-life"
        )

    subdir, suffix = {
        "command": ("commands", ".md"),
        "skill": ("skills", "/SKILL.md"),
        "agent": ("agents", ".md"),
    }[kind]
    protected = deployment / ".claude" / subdir / f"protected{suffix}"
    plain = deployment / ".claude" / subdir / f"plain{suffix}"
    install_args = ("install", "--no-policy", *(("--global",) if user_scope else ()))
    pending = run(*install_args)
    assert pending.returncode == 0, pending.stdout + pending.stderr
    assert plain.is_file(), pending.stdout + pending.stderr
    assert not protected.exists()
    assert "withheld" in pending.stdout.lower()

    def audit_unchanged() -> None:
        def snapshot() -> dict[Path, bytes]:
            return {
                path: path.read_bytes()
                for path in isolated.root.rglob("*")
                if path.is_file() and not path.is_relative_to(isolated.temp_root)
            }

        before = snapshot()
        audit = run("audit", "--ci", "--no-policy", "--format", "json")
        assert snapshot() == before
        assert audit.returncode == 0, audit.stdout + audit.stderr
        assert json.loads(audit.stdout)["passed"] is True

    audit_unchanged()
    approval = run("approve", identity)
    assert approval.returncode == 0, approval.stdout + approval.stderr
    allowed = run(*install_args)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert protected.is_file()
    assert load_frontmatter(protected).metadata["hooks"] == _HOOKS
    audit_unchanged()
    denied = run("deny", identity)
    assert denied.returncode == 0, denied.stdout + denied.stderr
    revoked = run(*install_args)
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    assert plain.is_file()
    assert not protected.exists(), revoked.stdout + revoked.stderr
    assert "restart" in revoked.stdout.lower()
    audit_unchanged()


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize(
    "definition",
    [
        [],
        {"PreToolUse": [{"hooks": [{"type": "command", "args": ["echo", "no"]}]}]},
        {"preToolUse": [{"hooks": [{"type": "command", "command": "echo no"}]}]},
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "python ./missing.py"}]}]},
    ],
    ids=["malformed", "unsupported-invocation", "foreign-event", "missing-resource"],
)
def test_invalid_inline_definition_fails_before_activation(
    tmp_path: Path, apm_binary_path: Path, kind: str, definition: object
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    source = _primitive(factory, dependency, kind, "protected", hooks=True)
    metadata = load_frontmatter(source).metadata
    metadata["hooks"] = definition
    source.write_text(f"---\n{yaml_to_str(metadata)}---\nDo not execute commands.\n")
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
        scenario_id=f"invalid-inline-{kind}",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "hook" in (result.stdout + result.stderr).lower()
    assert not list((consumer.root / ".claude").rglob("*.md"))
    assert not (consumer.root / ".claude/settings.json").exists()


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
def test_inline_withholding_does_not_change_other_target(
    tmp_path: Path, apm_binary_path: Path, kind: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    _primitive(factory, dependency, kind, "plain", hooks=False)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude", "copilot")
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, consumer.manifest_path)
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy"),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
        scenario_id=f"inline-target-control-{kind}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    subdir, suffix = {
        "command": ("prompts", ".prompt.md"),
        "skill": ("skills", "/SKILL.md"),
        "agent": ("agents", ".agent.md"),
    }[kind]
    native_root = ".agents" if kind == "skill" else ".github"
    unchanged = consumer.root / native_root / subdir / f"protected{suffix}"
    assert load_frontmatter(unchanged).metadata["hooks"] == _HOOKS
    assert not any(
        "protected" in path.as_posix() for path in (consumer.root / ".claude").rglob("*.md")
    )
    assert not (consumer.root / ".github/hooks").exists()


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("damage", ["edited", "missing-hash"])
def test_edited_inline_primitive_is_not_silently_revoked(
    tmp_path: Path, apm_binary_path: Path, kind: str, user_scope: bool, damage: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    identity = dependency.root.as_posix() if user_scope else "../guarded"
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"] = {"apm": [{"path": identity}]}
    dump_yaml(manifest, workspace / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    install_args = ("install", "--no-policy", *(("--global",) if user_scope else ()))
    installed = runner.run(install_args, cwd=workspace, env=isolated.subprocess_env())
    assert installed.returncode == 0, installed.stdout + installed.stderr
    [deployed] = list((deployment / ".claude").rglob("*.md"))
    if damage == "edited":
        deployed.write_text(deployed.read_text() + "\nPersonal changes.\n")
    else:
        lock_path = workspace / "apm.lock.yaml"
        document = load_yaml(lock_path)
        for dependency_record in document["dependencies"]:
            dependency_record.pop("deployed_file_hashes", None)
        for deployment_record in document["deployments"]:
            deployment_record["content_hash"] = None
        dump_yaml(document, lock_path)
        lock = LockFile.read(lock_path)
        assert lock is not None
        assert any(record.deployed_files for record in lock.dependencies.values())
        assert not any(record.deployed_file_hashes for record in lock.dependencies.values())
    retained = deployed.read_bytes()
    manifest["executables"] = {"deny": {identity: {"hooks": True}}}
    dump_yaml(manifest, workspace / "apm.yml")
    revoked = runner.run(install_args, cwd=workspace, env=isolated.subprocess_env())
    assert revoked.returncode != 0, revoked.stdout + revoked.stderr
    assert deployed.read_bytes() == retained
    diagnostic = " ".join(revoked.stdout.split())
    assert "revocation is incomplete" in diagnostic
    assert "restart" in diagnostic.lower()
    if damage == "missing-hash":
        assert "no recorded APM content hash" in diagnostic, revoked.stdout + revoked.stderr
    before_audit = {
        path: path.read_bytes()
        for root in (workspace, deployment / ".claude")
        for path in root.rglob("*")
        if path.is_file()
    }
    audit = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        cwd=workspace,
        env=isolated.subprocess_env(),
    )
    assert audit.returncode != 0, audit.stdout + audit.stderr
    assert all(path.read_bytes() == content for path, content in before_audit.items())


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
def test_first_party_inline_hooks_do_not_need_dependency_approval(
    tmp_path: Path, apm_binary_path: Path, kind: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    consumer = factory.create("consumer", targets=("claude",))
    source = _primitive(factory, consumer, kind, "protected", hooks=True)
    if kind == "skill":
        local = consumer.root / ".apm/skills/protected/SKILL.md"
        local.parent.mkdir(parents=True)
        source.rename(local)
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, consumer.manifest_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    [deployed] = list((consumer.root / ".claude").rglob("*.md"))
    assert load_frontmatter(deployed).metadata["hooks"] == _HOOKS
    audited = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert audited.returncode == 0, audited.stdout + audited.stderr


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("approved", [False, True])
def test_inline_json_and_opaque_plugin_controls_remain_separate(
    tmp_path: Path, apm_binary_path: Path, kind: str, user_scope: bool, approved: bool
) -> None:
    """Configuration-only witnesses never enable or invoke a native plugin."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    _primitive(factory, dependency, kind, "plain", hooks=False)
    global_hooks = deepcopy(_HOOKS)
    global_hooks["PreToolUse"][0]["hooks"][0]["command"] = "echo json-hook-control"
    factory.add_hook(dependency, "control", {"hooks": global_hooks})
    plugin = write_agent_plugin(isolated.work_root / "opaque", name="opaque", with_mcp=False)
    ignored = plugin / "agents/ignored.md"
    ignored.parent.mkdir()
    ignored.write_text("---\nname: ignored\nhooks: opaque-native-field\n---\nOpaque content.\n")
    original = {
        path.relative_to(plugin): path.read_bytes() for path in plugin.rglob("*") if path.is_file()
    }
    consumer = factory.create("consumer", targets=("claude",))
    workspace = isolated.config_root if user_scope else consumer.root
    deployment = isolated.home if user_scope else consumer.root
    identity = dependency.root.as_posix() if user_scope else "../guarded"
    plugin_identity = plugin.as_posix() if user_scope else "../opaque"
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"] = {"apm": [{"path": identity}, {"path": plugin_identity}]}
    manifest["executables"] = {"allow": {identity: {"hooks": True}} if approved else {}}
    dump_yaml(manifest, workspace / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    result = runner.run(
        ("install", "--no-policy", *(("--global",) if user_scope else ())),
        cwd=workspace,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    markdown = list((deployment / ".claude").rglob("*.md"))
    assert any("plain" in path.as_posix() for path in markdown)
    protected = [path for path in markdown if "protected" in path.as_posix()]
    assert bool(protected) is approved
    if approved:
        assert load_frontmatter(protected[0]).metadata["hooks"] == _HOOKS
    assert not any(
        "ignored" in path.as_posix() or "opaque-skill" in path.as_posix() for path in markdown
    )
    settings_path = deployment / ".claude/settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    assert not settings.get("enabledPlugins")
    assert bool(settings.get("hooks")) is approved
    if approved:
        commands = [
            handler["command"]
            for group in settings["hooks"]["PreToolUse"]
            for handler in group["hooks"]
        ]
        assert commands == ["echo json-hook-control"]
    lock = LockFile.read(workspace / "apm.lock.yaml")
    assert lock is not None
    record = lock.get_dependency(plugin_identity)
    assert record is not None
    materialized = record.to_dependency_ref().get_install_path(workspace / "apm_modules")
    assert all(
        (materialized / relative).read_bytes() == content for relative, content in original.items()
    )
    audited = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        cwd=workspace,
        env=isolated.subprocess_env(),
    )
    assert audited.returncode == 0, audited.stdout + audited.stderr


@pytest.mark.parametrize("selection", ["skill-subset", "other-target"])
def test_approval_inventory_uses_actual_selected_inline_sources(
    tmp_path: Path, apm_binary_path: Path, selection: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, "skill", "protected", hooks=True)
    _primitive(factory, dependency, "skill", "plain", hooks=False)
    requirement = {"path": "../guarded"}
    requirement["skills" if selection == "skill-subset" else "targets"] = (
        ["plain"] if selection == "skill-subset" else ["copilot"]
    )
    consumer = factory.create(
        "consumer", dependencies=(requirement,), targets=("claude", "copilot")
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, consumer.manifest_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    approved = runner.run(
        ("approve", "../guarded"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert load_yaml(consumer.manifest_path)["executables"]["allow"] == {}


@pytest.mark.parametrize("edited_resource", ["guard.py", "package.json"])
def test_inline_resource_edits_block_reinstallation_without_overwrite(
    tmp_path: Path, apm_binary_path: Path, edited_resource: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    source = _primitive(factory, dependency, "command", "protected", hooks=True)
    extension = "py" if edited_resource == "guard.py" else "js"
    script = source.parent / f"guard.{extension}"
    script.write_text("/* harmless fixture, never invoked */\n" if extension == "js" else "pass\n")
    document = deepcopy(_HOOKS)
    document["PreToolUse"][0]["hooks"][0]["command"] = (
        f"{'python' if extension == 'py' else 'node'} ./guard.{extension}"
    )
    source.write_text(f"---\n{yaml_to_str({'description': 'Harmless', 'hooks': document})}---\n")
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    [resource] = list((consumer.root / ".claude/hooks").rglob(edited_resource))
    edited = "personal resource content\n"
    resource.write_text(edited)
    reinstalled = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert reinstalled.returncode != 0, reinstalled.stdout + reinstalled.stderr
    assert resource.read_text() == edited
    assert "edited since deployment" in reinstalled.stdout + reinstalled.stderr
