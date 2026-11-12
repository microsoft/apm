"""Installed-CLI contract: ``apm approve`` unlocks github.com dependencies.

With the executable gate enabled (``executables: {}``), a dependency hosted
on the default host (github.com) parks its hooks pending approval. The
remediation APM prints -- ``apm approve <owner/repo>`` -- must make the next
``apm install`` deploy the hooks and record ``exec_status: deployed``,
whichever spelling the manifest uses for the default host. ``apm policy
explain``, ``apm approve --pending`` and the install gate must agree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_REMOTE = "https://github.com/acme/hooky.git"
_HOOK_COMMAND = "echo approved-hook"


def _hook_package(root: Path) -> None:
    (root / ".apm" / "hooks").mkdir(parents=True)
    dump_yaml({"name": "hooky", "version": "1.0.0"}, root / "apm.yml")
    document = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": _HOOK_COMMAND}]}
            ]
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
    ("spelling", "approve_args"),
    [
        ("acme/hooky#v1.0.0", ("approve", "acme/hooky")),
        ("https://github.com/acme/hooky.git#v1.0.0", ("approve", "--user", "acme/hooky")),
    ],
    ids=["shorthand-project-grant", "https-user-grant"],
)
def test_approve_unlocks_default_host_dependency_hooks(
    tmp_path: Path,
    apm_binary_path: Path,
    spelling: str,
    approve_args: tuple[str, ...],
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    source = isolated.package_root / "hooky"
    _hook_package(source)
    repository = repositories.create("hooky", source_tree=source)
    repositories.tag(repository, "v1.0.0", repositories.commit(repository, message="v1"))
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "target": "claude",
            "dependencies": {"apm": [spelling]},
            "executables": {},
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
    assert "hooks skipped" in gated
    assert _HOOK_COMMAND not in _claude_hook_commands(project)
    assert _exec_status(project) == "gated_pending_approval"

    apm(*approve_args)
    explained = apm("policy", "explain", "acme/hooky")
    assert "verdict: allowed" in explained, explained

    approved = apm("install", "--no-policy")
    assert "hooks skipped" not in approved, approved
    assert _claude_hook_commands(project) == [_HOOK_COMMAND]
    assert _exec_status(project) == "deployed"
    assert "acme/hooky" not in apm("approve", "--pending")

    rerun = apm("install", "--no-policy")
    assert "hooks skipped" not in rerun
    assert _claude_hook_commands(project) == [_HOOK_COMMAND]
