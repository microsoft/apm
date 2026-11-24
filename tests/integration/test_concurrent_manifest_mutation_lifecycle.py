"""Installed-CLI contract: racing manifest mutations converge.

Two real ``apm`` processes mutate the same project at once -- one adds a
package, the other removes a different one. The lifecycle lock must
serialize them so the final state honours BOTH intents: apm.yml, the
lockfile and the deployed files agree, ``apm audit --ci`` is clean and a
rerun is a byte-for-byte no-op.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary, pytest.mark.e2e]

_PACKAGES = ("alpha", "beta", "gamma")


def _package(root: Path, name: str) -> None:
    rules = root / ".apm" / "instructions"
    rules.mkdir(parents=True)
    dump_yaml({"name": name, "version": "1.0.0"}, root / "apm.yml")
    (rules / f"{name}.instructions.md").write_text(
        f'---\napplyTo: "**"\n---\n# {name} rule\n', encoding="ascii"
    )


def _deployed(project: Path) -> set[str]:
    return {path.name for path in (project / ".github" / "instructions").glob("*.md")}


def test_concurrent_install_and_uninstall_converge(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    for name in _PACKAGES:
        _package(project / "pkgs" / name, name)
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "target": "copilot",
            "dependencies": {"apm": ["./pkgs/alpha", "./pkgs/beta"]},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _deployed(project) == {"alpha.instructions.md", "beta.instructions.md"}

    commands = (
        ("install", "--no-policy", "./pkgs/gamma"),
        ("uninstall", "./pkgs/beta"),
    )
    processes = [
        subprocess.Popen(
            [str(apm_binary_path), *args],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for args in commands
    ]
    try:
        outputs = [process.communicate(timeout=300) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    for args, process, (stdout, stderr) in zip(commands, processes, outputs, strict=True):
        assert process.returncode == 0, f"apm {' '.join(args)}\n{stdout}{stderr}"

    manifest = load_yaml(project / "apm.yml")
    assert manifest["dependencies"]["apm"] == ["./pkgs/alpha", "./pkgs/gamma"]
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    assert sorted(dep.local_path for dep in lock.dependencies.values()) == [
        "./pkgs/alpha",
        "./pkgs/gamma",
    ]
    assert _deployed(project) == {"alpha.instructions.md", "gamma.instructions.md"}

    audit = runner.run(("audit", "--ci", "--no-policy"), cwd=project, env=env)
    assert audit.returncode == 0, audit.stdout + audit.stderr

    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    manifest_bytes = (project / "apm.yml").read_bytes()
    rerun = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (project / "apm.yml").read_bytes() == manifest_bytes


_USER_SETTINGS = {"permissions": {"allow": ["Bash(ls)"]}}


def _hook_package(root: Path, name: str) -> None:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    dump_yaml({"name": name, "version": "1.0.0"}, root / "apm.yml")
    handler = {"type": "command", "command": f"echo {name}"}
    document = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [handler]}]}}
    (hooks / f"{name}.json").write_text(json.dumps(document), encoding="ascii")


def _hook_project(tmp_path: Path, apm_binary_path: Path, target: str = "claude"):
    """A *target* project with alpha installed; beta and gamma ready to add."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    for name in _PACKAGES:
        _hook_package(project / "pkgs" / name, name)
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(_USER_SETTINGS), encoding="ascii")
    allow = {f"./pkgs/{name}": {"hooks": True} for name in _PACKAGES}
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "targets": [target],
            "dependencies": {"apm": ["./pkgs/alpha"]},
            "executables": {"allow": allow},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    return runner, env, project, settings


def _hook_commands(settings: Path) -> list[str]:
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler["command"] for entry in data["hooks"]["PreToolUse"] for handler in entry["hooks"]
    ]


def test_concurrent_hook_installs_keep_every_shared_settings_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Cut probe (#5098, concurrency): two racing installs into one shared
    .claude/settings.json serialize and keep every package's hook and the
    user's own settings."""
    _runner, env, project, settings = _hook_project(tmp_path, apm_binary_path)
    commands = (
        ("install", "--no-policy", "./pkgs/beta"),
        ("install", "--no-policy", "./pkgs/gamma"),
    )
    processes = [
        subprocess.Popen(
            [str(apm_binary_path), *args],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for args in commands
    ]
    outputs = [process.communicate(timeout=300)[0] for process in processes]
    for process, text in zip(processes, outputs, strict=True):
        assert process.returncode == 0, text
    assert sorted(_hook_commands(settings)) == ["echo alpha", "echo beta", "echo gamma"]
    assert (
        json.loads(settings.read_text(encoding="utf-8"))["permissions"]
        == (_USER_SETTINGS["permissions"])
    )


def _project_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and not {"apm_modules", "pkgs"} & set(path.relative_to(project).parts)
    }


def _mcp_package(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    server = {
        "name": f"srv-{name}",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "args": [name],
    }
    dump_yaml(
        {"name": name, "version": "1.0.0", "dependencies": {"mcp": [server]}}, root / "apm.yml"
    )


def _lsp_package(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    server = {
        "name": f"{name}-server",
        "command": "true",
        "args": ["--stdio"],
        "extensionToLanguage": {f".{name}": name},
    }
    dump_yaml(
        {"name": name, "version": "1.0.0", "dependencies": {"lsp": [server]}}, root / "apm.yml"
    )


_MERGED_KINDS = {"mcp": _mcp_package, "lsp": _lsp_package}


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("mcp", "claude"),
        ("mcp", "copilot"),
        ("mcp", "cursor"),
        ("lsp", "copilot"),
        ("lsp", "claude"),
        ("lsp", "opencode"),
    ],
)
def test_incremental_merged_config_installs_in_any_order_pass_audit(
    tmp_path: Path, apm_binary_path: Path, kind: str, target: str
) -> None:
    """Cut probe (#5285 sibling): MCP and LSP servers merged into one config by
    incremental installs converge with a full install (I2, I3)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    for name in _PACKAGES:
        _MERGED_KINDS[kind](project / "pkgs" / name, name)
    allow = {f"./pkgs/{name}": {kind: True} for name in _PACKAGES}
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "targets": [target],
            "dependencies": {"apm": ["./pkgs/alpha"]},
            "executables": {"allow": allow},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)
    trust = ("--no-policy", "--trust-transitive-mcp")
    for extra in ((), ("./pkgs/gamma",), ("./pkgs/beta",)):
        added = runner.run(("install", *trust, *extra), cwd=project, env=env)
        assert added.returncode == 0, added.stdout + added.stderr
    before = _project_bytes(project)
    # Content oracle: one deployed config holds all three merged servers.
    names = [f"srv-{n}" if kind == "mcp" else f"{n}-server" for n in _PACKAGES]
    configs = {
        path: body
        for path, body in before.items()
        if path not in {"apm.yml", "apm.lock.yaml"} and all(n.encode() in body for n in names)
    }
    assert configs, f"no deployed config names all of {names}: {sorted(before)}"
    audit = runner.run(("audit", "--ci", "--no-policy"), cwd=project, env=env)
    assert audit.returncode == 0, audit.stdout[-1200:]
    rerun = runner.run(("install", *trust), cwd=project, env=env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert _project_bytes(project) == before


@pytest.mark.parametrize(
    "target",
    ["copilot", "claude", "cursor", "codex", "gemini"],
)
def test_incremental_hook_installs_in_any_order_pass_audit(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """#5285: ``apm install <pkg>`` additions out of name order leave a converged
    project: ``audit --ci`` agrees and a plain install changes no byte (I2, I3)."""
    runner, env, project, _settings = _hook_project(tmp_path, apm_binary_path, target)
    for name in ("gamma", "beta"):
        added = runner.run(("install", "--no-policy", f"./pkgs/{name}"), cwd=project, env=env)
        assert added.returncode == 0, added.stdout + added.stderr
    before = _project_bytes(project)
    audit = runner.run(("audit", "--ci", "--no-policy"), cwd=project, env=env)
    rerun = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    after = _project_bytes(project)
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    assert (changed, audit.returncode) == ([], 0), audit.stdout[-1200:]
