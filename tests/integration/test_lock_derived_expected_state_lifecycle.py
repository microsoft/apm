"""Installed-CLI contracts: install derives expected state from the lock.

Machine-local facts never decide what an install expects; apm.yml,
apm.lock.yaml and the locked sources do:

* A stale ``apm_modules`` copy left by an older lock (#3884): the resolver
  must not read its outdated ``apm.yml`` and widen a locked skill subset.
* A fresh clone without deployment receipts whose ``apm.yml`` ref was bumped
  (the Renovate flow, #3883) adopts committed skill output that is
  byte-identical to what the currently locked version renders, then installs
  the new version. A hand-edited committed file still fails closed.
* Claude plugin ``${CLAUDE_PLUGIN_ROOT}`` MCP/LSP state recorded portably
  (#3825): a teammate's clone at another path replays ``apm install
  --frozen`` and ``apm audit --ci`` without rewriting committed files.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._lockfile_chain_kit import output, skill
from tests.integration._path_corner_world_kit import (
    CornerWorld,
    remote_url,
    tree_bytes,
    write,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]


def _url(name: str) -> str:
    return f"https://github.com/test/{name}.git"


def _consumer(root: Path, parent_ref: str) -> Path:
    root.mkdir(parents=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [{"git": _url("parent"), "ref": parent_ref}]},
        },
        root / "apm.yml",
    )
    return root


def test_stale_module_copy_keeps_locked_skill_subset_on_first_frozen_install(
    tmp_path, apm_binary_path
) -> None:
    """#3884: a copy from the previous lock is refreshed before resolution reads it."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())

    source = isolated.package_root / "bundle"
    source.mkdir(parents=True)
    dump_yaml({"name": "bundle", "version": "1.0.0"}, source / "apm.yml")
    write(source / "skills/alpha/SKILL.md", skill("alpha", "Alpha."))
    write(source / "skills/beta/SKILL.md", skill("beta", "Beta."))
    bundle = factory.create("bundle", source_tree=source)
    factory.tag(bundle, "v1", factory.commit(bundle, message="bundle"))

    source = isolated.package_root / "parent"
    write(source / ".apm/instructions/parent.instructions.md", "---\napplyTo: '**'\n---\nP.\n")
    manifest = {
        "name": "parent",
        "version": "1.0.0",
        "dependencies": {"apm": [{"git": _url("bundle"), "ref": "v1"}]},
    }
    dump_yaml(manifest, source / "apm.yml")
    parent = factory.create("parent", source_tree=source)
    factory.tag(parent, "v1", factory.commit(parent, message="all skills"))
    manifest["version"] = "1.1.0"
    manifest["dependencies"]["apm"][0]["skills"] = ["alpha"]
    dump_yaml(manifest, parent.worktree / "apm.yml")
    factory.tag(parent, "v2", factory.commit(parent, message="alpha only"))

    env = factory.url_rewrite_subprocess_env_many(
        ((bundle, _url("bundle")), (parent, _url("parent")))
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    install = ("install", "--no-policy", "--parallel-downloads", "0")

    project = _consumer(isolated.work_root / "consumer", "v1")
    first = runner.run(install, cwd=project, env=env)
    assert first.returncode == 0, output(first)
    stale_copy = project / "apm_modules/test/parent/apm.yml"
    assert "skills" not in stale_copy.read_text(encoding="utf-8")

    # A teammate bumps the parent and commits the new lock; this checkout
    # pulls apm.yml + apm.lock.yaml but keeps its previous apm_modules copy.
    upstream = _consumer(isolated.work_root / "upstream", "v2")
    bumped = runner.run(install, cwd=upstream, env=env)
    assert bumped.returncode == 0, output(bumped)
    lock = (upstream / "apm.lock.yaml").read_bytes()
    locked = {dep["repo_url"]: dep for dep in load_yaml(upstream / "apm.lock.yaml")["dependencies"]}
    assert locked["test/bundle"]["skill_subset"] == ["alpha"]
    for name in ("apm.yml", "apm.lock.yaml"):
        (project / name).write_bytes((upstream / name).read_bytes())

    frozen = runner.run((*install, "--frozen"), cwd=project, env=env)
    assert frozen.returncode == 0, output(frozen)
    assert "selections differ" not in output(frozen)
    assert (project / "apm.lock.yaml").read_bytes() == lock
    assert load_yaml(stale_copy)["dependencies"]["apm"][0]["skills"] == ["alpha"]
    assert (project / ".agents/skills/alpha/SKILL.md").is_file()


_ROOTS = (".agents/skills", ".claude/skills")
_REFUSAL = "no trustworthy local deployment baseline"
_REMOTE = "https://github.com/test/skills-source.git"


@dataclass(frozen=True)
class _BumpedClone:
    env: dict[str, str]
    runner: ApmLifecycleRunner
    clone: Path
    v2_sha: str


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def _alpha(body: str) -> str:
    return f"---\nname: alpha\ndescription: Alpha\n---\n{body} [tools](../beta/references/x.md).\n"


def _bumped_clone(tmp_path: Path, apm_binary_path: Path) -> _BumpedClone:
    """Commit v1 output, clone it fresh, evict receipts, and bump apm.yml to v2."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    source = isolated.package_root / "skills-source"
    _write(source / "skills/alpha/SKILL.md", _alpha("Use"))
    _write(source / "skills/beta/SKILL.md", "---\nname: beta\ndescription: Beta\n---\nBeta.\n")
    _write(source / "skills/beta/references/x.md", "Tools.\n")
    repository = repositories.create("skills-source", source_tree=source)
    v1 = repositories.commit(repository, message="v1")
    repositories.tag(repository, "v1", v1)
    _write(repository.worktree / "skills/alpha/SKILL.md", _alpha("Now use"))
    v2 = repositories.commit(repository, message="v2")
    repositories.tag(repository, "v2", v2)
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude", "copilot"],
            "dependencies": {"apm": [{"git": _REMOTE, "ref": "v1"}]},
        },
        project / "apm.yml",
    )
    _write(project / ".gitignore", "apm_modules/\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    rendered = (project / ".claude/skills/alpha/SKILL.md").read_text(encoding="ascii")
    assert "](../beta/references/x.md)" not in rendered, "fixture must exercise link rendering"
    _git(project, env, "init", "--initial-branch=main")
    _git(project, env, "-c", "user.email=t@example.com", "-c", "user.name=T", "add", "--all")
    _git(project, env, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "v1")

    clone = isolated.work_root / "fresh-clone"
    _git(isolated.work_root, env, "clone", "--quiet", str(project), str(clone))
    for receipts in (isolated.home / ".apm").glob("deployment-receipts*"):
        shutil.rmtree(receipts)
    shutil.rmtree(isolated.cache_root, ignore_errors=True)
    isolated.cache_root.mkdir(parents=True)
    manifest = clone / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("ref: v1", "ref: v2"), encoding="utf-8"
    )
    return _BumpedClone(env, runner, clone, v2.sha)


def test_ref_bump_on_fresh_clone_adopts_locked_output_and_fails_closed_on_edits(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Committed v1 output is adopted and replaced by v2; an edited copy is kept."""
    bumped = _bumped_clone(tmp_path, apm_binary_path)
    clone = bumped.clone
    edited_clone = clone.with_name("edited-clone")
    shutil.copytree(clone, edited_clone)

    result = bumped.runner.run(("install", "--no-policy"), cwd=clone, env=bumped.env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, output
    assert _REFUSAL not in output
    assert bumped.v2_sha in (clone / "apm.lock.yaml").read_text(encoding="utf-8")
    for root in _ROOTS:
        alpha = (clone / root / "alpha/SKILL.md").read_text(encoding="ascii")
        assert "Now use [tools](" in alpha and "](../beta/references/x.md)" not in alpha
    changed = {line[3:] for line in _git(clone, bumped.env, "status", "--porcelain").splitlines()}
    assert changed == {"apm.yml", "apm.lock.yaml", *(f"{r}/alpha/SKILL.md" for r in _ROOTS)}

    edited = edited_clone / ".claude/skills/alpha/SKILL.md"
    edited.write_text("Local edits.\n", encoding="ascii")
    lock_before = (edited_clone / "apm.lock.yaml").read_bytes()
    refused = bumped.runner.run(("install", "--no-policy"), cwd=edited_clone, env=bumped.env)
    refusal = " ".join((refused.stdout + refused.stderr).split())
    assert refused.returncode != 0, refusal
    assert _REFUSAL in refusal
    assert edited.read_text(encoding="ascii") == "Local edits.\n"
    assert (edited_clone / "apm.lock.yaml").read_bytes() == lock_before


_ROOT = "${CLAUDE_PLUGIN_ROOT}"
_COMMITTED = (".git", "apm_modules")


def _claude_plugin(root: Path, name: str) -> Path:
    write(
        root / ".claude-plugin/plugin.json",
        json.dumps({"name": name, "version": "1.0.0", "description": "plugin root fixture"}),
    )
    write(
        root / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    f"{name}-db": {
                        "command": "node",
                        "args": [f"{_ROOT}/scripts/server.js"],
                        "env": {"DATA": f"{_ROOT}/data"},
                    }
                }
            }
        ),
    )
    write(
        root / ".lsp.json",
        json.dumps(
            {
                "lspServers": {
                    f"{name}-ls": {
                        "command": f"{_ROOT}/scripts/server.js",
                        "args": ["--stdio"],
                        "extensionToLanguage": {f".{name}": name},
                    }
                }
            }
        ),
    )
    write(root / "scripts/server.js", "// inert fixture\n")
    return root


def test_plugin_root_mcp_and_lsp_replay_frozen_in_a_moved_clone(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = CornerWorld.create(tmp_path, apm_binary_path)
    world.publish("synth", _claude_plugin(world.isolated.package_root / "synth", "synth"))
    _claude_plugin(world.isolated.work_root / "local-synth", "loco")
    project = world.project(
        "first clone",
        targets=("claude",),
        deps=(remote_url("synth"), {"path": "../local-synth"}),
    )
    (project / ".claude").mkdir()
    world.apm(project, "install", "--no-policy")

    lock = world.lock(project)
    assert lock["mcp_configs"]["synth-db"]["args"] == [f"{_ROOT}/scripts/server.js"]
    assert lock["mcp_configs"]["loco-db"]["env"] == {"DATA": f"{_ROOT}/data"}
    assert lock["lsp_configs"]["synth-ls"]["command"] == f"{_ROOT}/scripts/server.js"
    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["synth-db"]["args"] == [
        "${CLAUDE_PROJECT_DIR:-.}/apm_modules/acme/synth/scripts/server.js"
    ]
    assert servers["loco-db"]["env"] == {
        "DATA": "${CLAUDE_PROJECT_DIR:-.}/apm_modules/_local/local-synth/data"
    }
    lsp_plugin = project / ".claude/skills/apm-lsp/.claude-plugin/plugin.json"
    lsp = json.loads(lsp_plugin.read_text(encoding="utf-8"))["lspServers"]
    assert lsp["synth-ls"]["command"] == (
        "${CLAUDE_PROJECT_DIR}/apm_modules/acme/synth/scripts/server.js"
    )
    committed = tree_bytes(project, exclude=_COMMITTED)
    assert str(project) not in "".join(
        text.decode("utf-8", "replace") for text in committed.values()
    )

    moved = world.isolated.work_root / "teammate clone"
    shutil.copytree(project, moved, symlinks=True)
    shutil.rmtree(moved / "apm_modules")

    world.apm(moved, "install", "--frozen", "--no-policy")
    world.apm(moved, "audit", "--ci", "--no-policy")
    assert tree_bytes(moved, exclude=_COMMITTED) == committed

    world.apm(moved, "install", "--no-policy")
    assert tree_bytes(moved, exclude=_COMMITTED) == committed


_NATIVE_REFERENCES = {
    ".mcp.json": ("claude", "${CLAUDE_PROJECT_DIR:-.}"),
    ".cursor/mcp.json": ("cursor", "${workspaceFolder}"),
}


def _expand(value: object, root: str) -> object:
    if isinstance(value, str):
        return value.replace(_ROOT, root)
    if isinstance(value, dict):
        return {key: _expand(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, root) for item in value]
    return value


def _rewrite_as_pre_3825_release(project: Path, *, fingerprints: bool) -> None:
    """Rewrite MCP state as releases before #3825 wrote it: the root expanded."""
    from apm_cli.factory import ClientFactory
    from apm_cli.install.mcp.ownership import mcp_native_hash

    relative = "apm_modules/acme/synth"
    root = str((project / relative).resolve())
    hashes: dict[str, str] = {}
    for path, (runtime, reference) in _NATIVE_REFERENCES.items():
        native = project / path
        text = native.read_text(encoding="utf-8").replace(
            f"{reference}/{relative}", json.dumps(root)[1:-1]
        )
        native.write_text(text, encoding="utf-8")
        entry = json.loads(text)["mcpServers"]["synth-db"]
        client = ClientFactory.create_client(runtime, project_root=project)
        hashes[runtime] = mcp_native_hash(client.managed_server_config(entry))
    lock = load_yaml(project / "apm.lock.yaml")
    lock["mcp_configs"]["synth-db"] = _expand(lock["mcp_configs"]["synth-db"], root)
    for record in lock["deployments"]:
        if record.get("kind") == "uri" and record.get("target") == "mcp":
            record["content_hash"] = hashes.get(record["runtime"]) if fingerprints else None
    dump_yaml(lock, project / "apm.lock.yaml")


def _pre_3825_project(tmp_path: Path, apm_binary_path: Path, *, fingerprints: bool):
    world = CornerWorld.create(tmp_path, apm_binary_path)
    world.publish("synth", _claude_plugin(world.isolated.package_root / "synth", "synth"))
    project = world.project("legacy", targets=("claude", "cursor"), deps=(remote_url("synth"),))
    (project / ".claude").mkdir()
    (project / ".cursor").mkdir()
    world.apm(project, "install", "--no-policy")
    _rewrite_as_pre_3825_release(project, fingerprints=fingerprints)
    shutil.rmtree(project / "apm_modules")
    return world, project


def _servers(project: Path, path: str) -> dict:
    return json.loads((project / path).read_text(encoding="utf-8"))["mcpServers"]


def test_frozen_replay_of_a_pre_3825_lock_keeps_native_state_removable(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, project = _pre_3825_project(tmp_path, apm_binary_path, fingerprints=True)
    recorded = {
        path: (project / path).read_bytes() for path in (*_NATIVE_REFERENCES, "apm.lock.yaml")
    }

    world.apm(project, "install", "--frozen", "--no-policy")
    world.apm(project, "audit", "--ci", "--no-policy")
    assert {path: (project / path).read_bytes() for path in recorded} == recorded

    world.apm(project, "uninstall", remote_url("synth"))
    for path in _NATIVE_REFERENCES:
        assert "synth-db" not in _servers(project, path)


def test_install_migrates_an_unfingerprinted_pre_3825_entry_per_target(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, project = _pre_3825_project(tmp_path, apm_binary_path, fingerprints=False)

    world.apm(project, "install", "--no-policy")

    script = "apm_modules/acme/synth/scripts/server.js"
    assert world.lock(project)["mcp_configs"]["synth-db"]["args"] == [f"{_ROOT}/scripts/server.js"]
    for path, (_runtime, reference) in _NATIVE_REFERENCES.items():
        assert _servers(project, path)["synth-db"]["args"] == [f"{reference}/{script}"]
    world.apm(project, "install", "--frozen", "--no-policy")
    world.apm(project, "uninstall", remote_url("synth"))
    for path in _NATIVE_REFERENCES:
        assert "synth-db" not in _servers(project, path)
