"""Installed-CLI consent for native Claude inline hooks, without invoking Claude."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_frontmatter, load_yaml, yaml_to_str
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
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


@pytest.mark.parametrize("kind", ["command", "skill", "agent"])
def test_denied_inline_hook_withholds_whole_primitive(
    tmp_path: Path, apm_binary_path: Path, kind: str
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
    manifest["executables"] = {"deny": {"*": {"hooks": True}}}
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
    handler.update({"timeout": 5, "once": True})
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
        assert actual["hooks"][0]["once"] is True
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
def test_edited_inline_primitive_is_not_silently_revoked(
    tmp_path: Path, apm_binary_path: Path, kind: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "case", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("guarded")
    _primitive(factory, dependency, kind, "protected", hooks=True)
    consumer = factory.create(
        "consumer", dependencies=({"path": "../guarded"},), targets=("claude",)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    installed = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    [deployed] = list((consumer.root / ".claude").rglob("*.md"))
    edited = deployed.read_text() + "\nPersonal changes.\n"
    deployed.write_text(edited)
    manifest = load_yaml(consumer.manifest_path)
    manifest["executables"] = {"deny": {"*": {"hooks": True}}}
    dump_yaml(manifest, consumer.manifest_path)
    revoked = runner.run(
        ("install", "--no-policy"), cwd=consumer.root, env=isolated.subprocess_env()
    )
    assert revoked.returncode != 0, revoked.stdout + revoked.stderr
    assert deployed.read_text() == edited
    assert "revocation is incomplete" in revoked.stdout
    audit = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert audit.returncode != 0, audit.stdout + audit.stderr


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
    manifest["executables"] = {"deny": {"*": {"hooks": True}}}
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
