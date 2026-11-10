"""Real-binary contract: cold ``apm audit --ci`` replays SSH-declared deps over SSH.

A dependency declared as ``git@host:owner/repo.git`` installs over SSH. The
cold audit replay (no ``apm_modules/``, no cache) must fetch it over the same
declared transport instead of the shorthand HTTPS default (#3374). HTTPS is
made unreachable so a wrong transport fails loudly, and a logging SSH shim
proves the replay fetch actually used SSH.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_SKILL_TEXT = "---\nname: ssh-replay\ndescription: SSH replay fixture\n---\nHello over SSH.\n"
_CHILD_SKILL_TEXT = (
    "---\nname: ssh-child\ndescription: SSH transitive fixture\n---\nChild over SSH.\n"
)
_SIBLING_SKILL_TEXT = (
    "---\nname: ssh-sibling\ndescription: SSH relative-path fixture\n---\nSibling over SSH.\n"
)


def _write_ssh_shim(helper: Path, *, log: Path, origin: Path, git: str) -> None:
    """Write an ``ssh`` stand-in that logs each connection and serves *origin*."""
    helper.write_text(
        "import json, shlex, sys\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlparse\n"
        "args = sys.argv[1:]\n"
        "if '-G' in args:\n"
        "    sys.exit(0)\n"
        "host = urlparse('ssh://' + args[-2])\n"
        "command = shlex.split(args[-1])\n"
        "assert command[0] == 'git-upload-pack', args\n"
        "assert command[1].lstrip('/') == 'fixture/ssh-replay.git', args\n"
        f"with Path({str(log)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'host': host.hostname, 'user': host.username}) + '\\n')\n"
        "import os\n"
        f"os.execv({git!r}, [{git!r}, 'upload-pack', {str(origin)!r}])\n",
        encoding="ascii",
    )


def _ssh_events(log: Path) -> list[dict[str, str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _assert_ok(result: CommandResult) -> None:
    assert result.returncode == 0, f"{result.command}\n{result.stdout}\n{result.stderr}"


def test_cold_audit_ci_replays_scp_dependency_over_ssh(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Install then cold ``audit --ci`` both succeed with only SSH reachable."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env={})
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("ssh-replay")
    packages = LocalPackageFactory(repository.worktree / "packages")
    package = packages.create("ssh-pkg")
    packages.add_skill(package, "ssh-replay", _SKILL_TEXT)
    child = packages.create("ssh-child")
    packages.add_skill(child, "ssh-child", _CHILD_SKILL_TEXT)
    # Transitive child inherits the parent's declared SSH transport.
    manifest = load_yaml(package.manifest_path)
    sibling = packages.create("ssh-sibling")
    packages.add_skill(sibling, "ssh-sibling", _SIBLING_SKILL_TEXT)
    manifest["dependencies"] = {
        "apm": [{"git": "parent", "path": "packages/ssh-child"}, {"path": "../ssh-sibling"}]
    }
    dump_yaml(manifest, package.manifest_path)
    commit = repositories.commit(repository, message="Seed SSH replay fixture")

    git = shutil.which("git")
    assert git is not None
    ssh_log = isolated.root / "ssh.jsonl"
    helper = isolated.root / "ssh_shim.py"
    _write_ssh_shim(helper, log=ssh_log, origin=repository.origin, git=git)
    # Fixture-owned transport seam: only file and SSH are allowed, so any
    # HTTPS clone attempt fails, and Python HTTP goes to a dead proxy.
    environment.update(
        GIT_SSH_COMMAND=shlex.join([sys.executable, str(helper)]),
        GIT_SSH_VARIANT="ssh",
        GIT_ALLOW_PROTOCOL="file:ssh",
        HTTPS_PROXY="http://127.0.0.1:9",
        https_proxy="http://127.0.0.1:9",
        APM_PROGRESS="never",
        APM_DISABLE_UPDATE_CHECK="1",
    )

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {
                "apm": [
                    {
                        "git": "git@github.com:fixture/ssh-replay.git",
                        "path": "packages/ssh-pkg",
                        "ref": commit.sha,
                    }
                ],
                "mcp": [],
            },
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    installed = runner.run(
        ("install", "--no-policy"),
        scenario_id="ssh-replay-install",
        cwd=project,
        env=environment,
    )
    _assert_ok(installed)
    assert (project / ".claude" / "skills" / "ssh-replay" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == _SKILL_TEXT
    assert (project / ".claude" / "skills" / "ssh-child" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == _CHILD_SKILL_TEXT
    assert (project / ".claude" / "skills" / "ssh-sibling" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == _SIBLING_SKILL_TEXT
    locked = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert sorted(entry.get("virtual_path") for entry in locked) == [
        "packages/ssh-child",
        "packages/ssh-pkg",
        "packages/ssh-sibling",
    ]
    install_events = len(_ssh_events(ssh_log))
    assert install_events > 0

    # Clean checkout: no apm_modules and no cache, so audit must replay.
    shutil.rmtree(project / "apm_modules")
    shutil.rmtree(isolated.cache_root)
    isolated.cache_root.mkdir()

    audited = runner.run(
        ("audit", "--ci", "--no-policy"),
        scenario_id="ssh-replay-cold-audit",
        cwd=project,
        env=environment,
    )
    _assert_ok(audited)
    events = _ssh_events(ssh_log)
    assert len(events) > install_events, "cold audit replay did not fetch over SSH"
    assert all(event == {"host": "github.com", "user": "git"} for event in events)
