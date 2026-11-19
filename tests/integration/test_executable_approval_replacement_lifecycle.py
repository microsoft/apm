"""Installed-CLI contract: ``apm approve`` unlocks a replaced dependency (#3710).

The executable gate keys consent by the source a ``replace:`` rule selects, so
an upstream grant never unlocks the replacement. The remediation install
prints -- ``apm approve <original>`` -- must write that selected-source key so
the next ``apm install`` deploys the replacement's hooks, for a local path and
for a fork. ``apm deny <original>`` then re-gates the replacement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_UPSTREAM = "https://github.com/acme/hooky.git"
_FORK = "https://github.com/my-org/hooky-fork.git"
_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)
_UPSTREAM_COMMAND = "echo upstream-hook"
_REPLACEMENT_COMMAND = "echo replacement-hook"


def _hook_package(root: Path, command: str) -> None:
    (root / ".apm" / "hooks").mkdir(parents=True)
    dump_yaml({"name": "hooky", "version": "1.0.0"}, root / "apm.yml")
    document = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]
        }
    }
    (root / ".apm" / "hooks" / "guard.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="ascii"
    )


def _claude_hook_commands(project: Path) -> list[str]:
    settings = project / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler.get("command", "")
        for group in data.get("hooks", {}).get("PreToolUse", [])
        for handler in group.get("hooks", [])
    ]


def _exec_status(project: Path) -> str | None:
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


@pytest.mark.parametrize(
    ("kind", "expected_grant"),
    [("local", "../local-hooky"), ("fork", "my-org/hooky-fork#patched")],
    ids=["local-path", "fork"],
)
def test_approve_original_name_unlocks_replacement_hooks(
    tmp_path: Path,
    apm_binary_path: Path,
    kind: str,
    expected_grant: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    upstream_source = isolated.package_root / "hooky"
    _hook_package(upstream_source, _UPSTREAM_COMMAND)
    upstream = repositories.create("hooky", source_tree=upstream_source)
    repositories.tag(upstream, "v1.0.0", repositories.commit(upstream, message="v1"))
    rewrites = [(upstream, _UPSTREAM)]

    project = isolated.work_root / "consumer"
    project.mkdir()
    if kind == "local":
        _hook_package(isolated.work_root / "local-hooky", _REPLACEMENT_COMMAND)
        with_source: dict[str, str] = {"path": "../local-hooky"}
    else:
        fork_source = isolated.package_root / "hooky-fork"
        _hook_package(fork_source, _REPLACEMENT_COMMAND)
        fork = repositories.create("hooky-fork", source_tree=fork_source)
        repositories.tag(fork, "patched", repositories.commit(fork, message="fork"))
        rewrites.append((fork, _FORK))
        with_source = {"git": _FORK, "ref": "patched"}
    env = repositories.url_rewrite_subprocess_env_many(rewrites)

    dump_yaml(
        {
            "$schema": _SCHEMA,
            "name": "consumer",
            "version": "0.1.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["acme/hooky#v1.0.0"]},
            # An upstream grant must not unlock the selected source.
            "executables": {"allow": {"acme/hooky": {"hooks": True}}},
            "replace": [{"for": {"git": _UPSTREAM}, "with": with_source}],
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == 0, f"apm {' '.join(args)}\n{output}"
        return output

    gated = apm("install", "--no-policy")
    assert "hooks skipped" in gated, gated
    assert "apm approve acme/hooky" in " ".join(gated.split())
    assert _claude_hook_commands(project) == []
    assert _exec_status(project) == "gated_pending_approval"

    apm("approve", "acme/hooky")
    allow = load_yaml(project / "apm.yml")["executables"]["allow"]
    assert allow == {"acme/hooky": {"hooks": True}, expected_grant: {"hooks": True}}
    explained = apm("policy", "explain", "acme/hooky")
    assert "verdict: allowed" in explained, explained

    approved = apm("install", "--no-policy")
    assert "hooks skipped" not in approved, approved
    assert _claude_hook_commands(project) == [_REPLACEMENT_COMMAND]
    assert _exec_status(project) == "deployed"
    assert "hooky" not in apm("approve", "--pending")

    # Deny by the declared name re-gates the replacement and also blocks the
    # upstream, so removing the replace: rule cannot revive an old grant.
    apm("deny", "acme/hooky")
    executables = load_yaml(project / "apm.yml")["executables"]
    assert set(executables["deny"]) == {expected_grant, "acme/hooky"}
    assert executables.get("allow", {}) == {}

    denied = apm("install", "--no-policy")
    assert "hooks skipped" in denied, denied
    assert _exec_status(project) == "denied"
