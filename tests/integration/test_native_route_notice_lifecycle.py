"""APMLifecycle: install names each dependency it installs as a native plugin (#4998).

PROMISE: P-OWN-claude-native-plugin-registration -- when a dependency takes
the native plugin route, the install summary says so, names where native
plugins are reported not to load, and names the loose route; a dependency
deployed loose gets no such notice (source: issue #4998 accepted scope).
CELL: OP=install SRC=git PRIM=plugin(skills) SCOPE=project TGT=claude
ROUTE=native | loose(skills: [name])
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-claude-native-plugin-registration", "P-INV-I6", "P-INV-I9"),
]

_NOTICE = (
    "installed as a native plugin. Native plugins are reported not to load in "
    "git worktrees, in non-interactive claude -p runs (including "
    "claude-code-action) and in Copilot CLI 1.0.106. To deploy it loose instead, "
    "set native: false on this dependency in apm.yml."
)


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _plugin(factory: LocalGitRepositoryFactory, name: str):
    repository = factory.create(name)
    worktree = repository.worktree
    (worktree / ".claude-plugin").mkdir(parents=True)
    (worktree / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="ascii"
    )
    for skill in ("alpha", "beta"):
        path = worktree / "skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {skill}\ndescription: {skill}\n---\n# {skill}\n", "ascii")
    factory.commit(repository, message=name)
    return repository


def test_install_summary_names_only_the_natively_installed_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    native = _plugin(factory, "native-kit")
    loose = _plugin(factory, "loose-kit")
    env = factory.url_rewrite_subprocess_env_many(
        [
            (native, "https://github.com/fixture/native-kit.git"),
            (loose, "https://github.com/fixture/loose-kit.git"),
        ]
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "targets": ["claude"],
                "dependencies": {
                    "apm": [
                        {"git": "fixture/native-kit", "ref": "main"},
                        {"git": "fixture/loose-kit", "ref": "main", "skills": ["alpha"]},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180).run(
        ("install",), cwd=project, env=env
    )

    assert result.returncode == 0, _evidence(result)
    output = result.stdout + result.stderr
    assert output.isascii(), _evidence(result)
    lines = [" ".join(line.split()) for line in output.splitlines()]
    notices = [line for line in lines if "installed as a native plugin" in line]
    assert notices == [f"[!] [fixture/native-kit] {_NOTICE}"], _evidence(result)
    # The routes are what the notice claims.
    assert (project / ".agents/plugins/native-kit/.claude-plugin/plugin.json").is_file()
    assert (project / ".claude/skills/alpha/SKILL.md").is_file()
    assert not (project / ".agents/plugins/loose-kit").exists()


def test_global_install_notice_omits_the_project_only_loose_hint(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    native = _plugin(factory, "native-kit")
    env = factory.url_rewrite_subprocess_env(native, "https://github.com/fixture/native-kit.git")
    user_manifest = isolated.home / ".apm" / "apm.yml"
    user_manifest.parent.mkdir(parents=True, exist_ok=True)
    user_manifest.write_text(
        json.dumps(
            {
                "name": "user",
                "version": "1.0.0",
                "targets": ["claude"],
                "dependencies": {"apm": [{"git": "fixture/native-kit", "ref": "main"}]},
            }
        ),
        encoding="utf-8",
    )
    cwd = isolated.work_root / "anywhere"
    cwd.mkdir()

    result = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180).run(
        ("install", "-g"), cwd=cwd, env=env
    )

    assert result.returncode == 0, _evidence(result)
    output = result.stdout + result.stderr
    lines = [" ".join(line.split()) for line in output.splitlines()]
    notices = [line for line in lines if "installed as a native plugin" in line]
    assert notices == [f"[!] [fixture/native-kit] {_NOTICE}"], _evidence(result)
