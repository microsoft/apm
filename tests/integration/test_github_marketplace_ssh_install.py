"""Real-Git CLI coverage for explicitly SSH-registered GitHub marketplaces."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.auth import AuthResolver
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    "registered_url",
    [
        "ssh://git@github.com/fixture/private-mkt.git",
        "git@github.com:fixture/private-mkt.git",
    ],
    ids=["ssh-url", "scp"],
)
@pytest.mark.parametrize(
    "scenario", ["success", "success-named", "missing-path", "missing-ref", "ssh-denied"]
)
def test_github_ssh_marketplace_installs_without_https(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registered_url: str,
    scenario: str,
) -> None:
    """Registration, preflight, download and deployment use real Git over the SSH seam."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("private-mkt")
    packages = LocalPackageFactory(repository.worktree / "plugins")
    package = packages.create("alpha")
    skill_text = "---\nname: hello\ndescription: SSH fixture skill\n---\nHello from SSH.\n"
    packages.add_skill(package, "hello", skill_text)
    catalog = repository.worktree / ".claude-plugin"
    catalog.mkdir()
    plugin_source: str | dict[str, str] = "./plugins/alpha"
    if scenario == "missing-path":
        plugin_source = "./plugins/missing"
    elif scenario in {"success-named", "missing-ref"}:
        plugin_source = {
            "source": "github",
            "repo": "fixture/private-mkt",
            "path": "plugins/alpha",
            "ref": "main" if scenario == "success-named" else "missing-reference",
        }
    (catalog / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "ssh-fixture",
                "owner": {"name": "Fixture"},
                "plugins": [{"name": "alpha", "source": plugin_source}],
            }
        ),
        encoding="utf-8",
    )
    commit = repositories.commit(repository, message="Seed private marketplace fixture")
    git = shutil.which("git")
    assert git is not None
    ssh_log = isolated.root / "ssh.jsonl"
    deny_ssh = isolated.root / "deny-ssh"
    helper = isolated.root / "ssh.py"
    helper.write_text(
        "import json, os, shlex, sys\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlparse\n"
        "args = sys.argv[1:]\n"
        "if '-G' in args:\n"
        "    sys.exit(0)\n"
        "host = urlparse('ssh://' + args[-2])\n"
        "assert host.hostname == 'github.com' and host.username == 'git', args\n"
        "command = shlex.split(args[-1])\n"
        "assert command[0] == 'git-upload-pack', args\n"
        "assert command[1].lstrip('/') == 'fixture/private-mkt.git', args\n"
        f"with Path({str(ssh_log)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'host': host.hostname, 'user': host.username}) + '\\n')\n"
        f"if Path({str(deny_ssh)!r}).exists():\n"
        "    sys.stderr.write('git@github.com: Permission denied (publickey).\\n')\n"
        "    sys.exit(128)\n"
        f"os.execv({git!r}, [{git!r}, 'upload-pack', {str(repository.origin)!r}])\n",
        encoding="ascii",
    )
    environment.update(
        GIT_SSH_COMMAND=shlex.join([sys.executable, str(helper)]),
        GIT_SSH_VARIANT="ssh",
        GIT_ALLOW_PROTOCOL="file:ssh",
        APM_PROGRESS="never",
        APM_DISABLE_UPDATE_CHECK="1",
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    monkeypatch.chdir(project)
    clear_apm_yml_cache()
    runner = CliRunner()
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(AuthResolver, "_resolve_token", return_value=(None, "none", "basic")),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch(
            "requests.Session.request", side_effect=AssertionError("Unexpected HTTPS request")
        ) as http,
    ):
        added = runner.invoke(
            cli,
            ["marketplace", "add", registered_url, "--name", "ssh-fixture", "-r", commit.sha],
        )
        assert added.exit_code == 0, added.output
        before_install = len(ssh_log.read_text(encoding="utf-8").splitlines())
        if scenario == "ssh-denied":
            deny_ssh.touch()
        installed = runner.invoke(
            cli,
            ["install", "alpha@ssh-fixture", "--only=apm", "--no-policy", "--target", "claude"],
        )
        http.assert_not_called()

    events = [json.loads(line) for line in ssh_log.read_text(encoding="utf-8").splitlines()]
    assert len(events) > before_install
    assert all(event == {"host": "github.com", "user": "git"} for event in events)
    clear_apm_yml_cache()
    if scenario not in {"success", "success-named"}:
        assert installed.exit_code != 0, installed.output
        assert not list((project / ".claude" / "skills").glob("*/SKILL.md"))
        assert not (project / "apm.lock.yaml").exists()
        if scenario == "ssh-denied":
            assert "SSH key has read access" in " ".join(installed.output.split())
            assert "all probes failed" not in installed.output
        elif scenario == "missing-ref":
            assert "missing-reference" in installed.output
        else:
            assert "plugins/missing" in installed.output
        return
    assert installed.exit_code == 0, installed.output
    manifest = load_yaml(project / "apm.yml")
    dependency = manifest["dependencies"]["apm"][0]
    remote = urlparse("ssh://" + dependency["git"].replace(":", "/", 1))
    assert (remote.scheme, remote.hostname, remote.username) == ("ssh", "github.com", "git")
    assert remote.path == "/fixture/private-mkt.git"
    assert dependency["path"] == "plugins/alpha"
    locked = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert len(locked) == 1
    assert locked[0]["repo_url"] == "fixture/private-mkt"
    assert locked[0]["host"] == "github.com"
    assert locked[0]["resolved_commit"] == commit.sha
    assert locked[0]["resolved_ref"] == ("main" if scenario == "success-named" else commit.sha)
    skills = list((project / ".claude" / "skills").glob("*/SKILL.md"))
    assert len(skills) == 1
    assert skills[0].read_text(encoding="utf-8") == skill_text
    clear_apm_yml_cache()
