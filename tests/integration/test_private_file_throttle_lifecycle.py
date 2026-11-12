"""Real CLI/Git recovery from an injected GitHub API quota boundary."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.git_credential_shim import GitCredentialShimFactory
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke]

_REPOSITORY = "fixture-private/private-files"
_TOKEN = "fixture-gh-private-files"
_REF = "v1.0.0"
_CONTENTS = {
    "instructions/alpha.instructions.md": (
        b"---\napplyTo: '**'\ndescription: Alpha guidance\n---\n# Alpha from v1\n"
    ),
    "instructions/beta.instructions.md": (
        b"---\napplyTo: '**'\ndescription: Beta guidance\n---\n# Beta from v1\n"
    ),
}

# Fault only the HTTP boundary. CLI, resolver, private cache and sparse Git
# execute normally; the existing socket guard still blocks external networking.
_API_QUOTA_FAULT = """
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

def quota_response(self, request, **kwargs):
    parsed = urlparse(request.url)
    if parsed.hostname not in ("api.github.com", "raw.githubusercontent.com"):
        raise AssertionError("Unexpected HTTP host: " + str(parsed.hostname))
    response = requests.Response()
    response.request = request
    response.url = request.url
    response.status_code = 404 if parsed.hostname == "raw.githubusercontent.com" else 403
    response._content = b'{"message": "fixture unavailable"}'
    if response.status_code == 403:
        response.headers["X-RateLimit-Remaining"] = "0"
        response.headers["Retry-After"] = "0"
    with Path(os.environ["APM_TEST_HTTP_LOG"]).open("a", encoding="utf-8") as log:
        log.write(json.dumps({
            "host": parsed.hostname, "path": parsed.path,
            "status": response.status_code,
            "authenticated": "Authorization" in request.headers,
        }) + "\\n")
    return response

if os.environ.get("APM_TEST_HTTP_LOG") and sys.argv[0] == "-m":
    import requests
    requests.sessions.Session.send = quota_response
"""


@pytest.mark.parametrize("parallel_downloads", (0, 2))
def test_private_parent_files_recover_from_api_throttle_using_gh_login(
    tmp_path: Path, apm_engine_command: tuple[str, ...], parallel_downloads: int
) -> None:
    """Install inherited files at the exact tag through real authenticated Git."""
    git = shutil.which("git")
    assert git is not None
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "scenario",
        base_env={**os.environ, "LC_ALL": "id_ID.UTF-8", "LANGUAGE": "id_ID:id"},
    )
    base_env = isolated.subprocess_env()
    source = isolated.package_root / "private-files"
    LocalPackageFactory(source / "presets").create(
        "common",
        dependencies=tuple({"git": "parent", "path": path} for path in _CONTENTS),
        targets=("copilot",),
    )
    for path, content in _CONTENTS.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=base_env)
    repository = repositories.create("private-files", source_tree=source)
    selected = repositories.commit(repository, message="selected private files")
    repositories.tag(repository, _REF, selected)
    for path in _CONTENTS:
        (repository.worktree / path).write_bytes(b"# Wrong revision\n")
    repositories.commit(repository, message="unselected main revision")
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=(
            {
                "git": f"https://github.com/{_REPOSITORY}.git",
                "path": "presets/common",
                "ref": _REF,
            },
        ),
        targets=("copilot",),
    )
    http_log = isolated.root / "http.jsonl"
    guard = isolated.root / "network_guard/sitecustomize.py"
    guard.write_text(guard.read_text(encoding="utf-8") + _API_QUOTA_FAULT, encoding="utf-8")
    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root, real_git=Path(git), env=base_env
    )
    with server_factory.start(
        (repository,), private_repositories=(repository,), password=_TOKEN
    ) as server:
        shim = GitCredentialShimFactory(isolated.root / "shim").create(
            base_env=base_env,
            real_git=Path(git),
            remote_map={_REPOSITORY: server.remote_url(repository)},
            credential="must-not-reach-helper",
            gh_credential=_TOKEN,
        )
        env = {
            **shim.environment,
            "APM_TEST_HTTP_LOG": str(http_log),
            "APM_TEST_LOOPBACK_PORTS": str(urlparse(server.proxy_url).port),
            "GIT_ALLOW_PROTOCOL": "file:http:https",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "APM_E2E_TESTS": "1",
        }
        runner = ApmLifecycleRunner(apm_engine_command, scenario_timeout_seconds=240)
        result = runner.run(
            (
                "install",
                "--target",
                "copilot",
                "--no-policy",
                "--parallel-downloads",
                str(parallel_downloads),
            ),
            scenario_id=f"private-files-quota-recovery-{parallel_downloads}",
            cwd=consumer.root,
            env=env,
        )
        assert result.returncode == 0, (
            result.stdout + result.stderr + f"\nshim_events={shim.events()!r}"
        )

        # Independently enumerate deployed instructions, not just lockfile claims.
        deployed = {
            path.name: path.read_bytes()
            for path in (consumer.root / ".github/instructions").glob("*.instructions.md")
        }
        assert len(deployed) == len(_CONTENTS)
        for path, content in _CONTENTS.items():
            matches = [data for name, data in deployed.items() if Path(path).name in name]
            assert len(matches) == 1
            assert content in matches[0]
            assert b"Wrong revision" not in matches[0]

        lock = load_yaml(consumer.root / "apm.lock.yaml")
        entries = {entry["virtual_path"]: entry for entry in lock["dependencies"]}
        assert set(entries) == {"presets/common", *_CONTENTS}
        for path, content in _CONTENTS.items():
            entry = entries[path]
            assert entry["repo_url"] == _REPOSITORY
            assert entry["resolved_ref"] == _REF
            assert entry["resolved_commit"] == selected.sha
            assert entry["is_virtual"] is True
            assert len(entry["deployed_files"]) == 1
            assert content in (consumer.root / entry["deployed_files"][0]).read_bytes()

        http = [json.loads(line) for line in http_log.read_text().splitlines()]
        assert http
        assert all(not event["authenticated"] for event in http)
        for path in _CONTENTS:
            file_requests = [
                event for event in http if event["path"] == f"/repos/{_REPOSITORY}/contents/{path}"
            ]
            assert len(file_requests) == 1
            assert file_requests[0]["status"] == 403
        events = shim.events()
        assert not [event for event in events if event["event"] == "credential-fill"]
        gh_events = [event for event in events if event["event"] == "gh"]
        assert gh_events == [
            {"event": "gh", "command": ["auth", "token", "--hostname", "github.com"]}
        ]
        git_events = [event for event in events if event["event"] == "git" and event["remotes"]]
        assert git_events
        assert all(event["lc_all"] == "C" and event["language"] == "C" for event in git_events)
        assert all(not event["github_token_env"] for event in git_events)
        file_fetches = [event for event in git_events if event["command"] == "fetch"]
        assert len(file_fetches) == 2 * len(_CONTENTS)
        assert sum(event["auth_config_present"] for event in file_fetches) == len(_CONTENTS)
        assert all(
            remote == {"host": "github.com", "path": _REPOSITORY, "authenticated_url": False}
            for event in git_events
            for remote in event["remotes"]
        )
        assert any(not observation.accepted for observation in server.observations)
        assert any(
            observation.accepted and observation.authorization_present
            for observation in server.observations
        )
        assert _TOKEN not in result.stdout + result.stderr + http_log.read_text()
        lock_bytes = (consumer.root / "apm.lock.yaml").read_bytes()
        repeated = runner.run(
            ("install", "--target", "copilot", "--no-policy"),
            scenario_id="private-files-quota-repeat",
            cwd=consumer.root,
            env=env,
        )
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert (consumer.root / "apm.lock.yaml").read_bytes() == lock_bytes
        assert {
            path.name: path.read_bytes()
            for path in (consumer.root / ".github/instructions").glob("*.instructions.md")
        } == deployed
