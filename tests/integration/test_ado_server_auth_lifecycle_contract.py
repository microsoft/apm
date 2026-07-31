"""Installed-CLI lifecycle proof for Azure DevOps Server credential isolation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_ADO_HOST = "ado.corp.example.com"
_ADO_PORT = 8443
_ADO_ORGANIZATION = "DefaultCollection"
_ADO_PROJECT = "Platform"
_ADO_REPOSITORY = "ado-server-auth-contract"
_ADO_PAT = "ado-lifecycle-pat-sentinel"
_GITHUB_PAT = "github-lifecycle-pat-sentinel"
_GITHUB_TOKEN = "github-actions-token-sentinel"
_GH_TOKEN = "gh-cli-token-sentinel"
_SOURCE = (
    f"https://{_ADO_HOST}:{_ADO_PORT}/{_ADO_ORGANIZATION}/{_ADO_PROJECT}/_git/{_ADO_REPOSITORY}"
)
_DEPENDENCY = {
    "git": _SOURCE,
    "path": "skills/ado-server-auth",
    "ref": "main",
}
_SKILL_PATH = Path(".agents/skills/ado-server-auth/SKILL.md")
_INSTALL_ARGS = (
    "install",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
)
_UPDATE_ARGS = (
    "update",
    "--yes",
    "--target",
    "copilot",
    "--parallel-downloads",
    "0",
)
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")
_SECRET_VALUES = (_ADO_PAT, _GITHUB_PAT, _GITHUB_TOKEN, _GH_TOKEN)


def _skill_document(marker: str) -> str:
    """Return a valid skill document with one observable version marker."""
    return (
        "---\n"
        "name: ado-server-auth\n"
        "description: Azure DevOps Server auth lifecycle contract\n"
        "---\n"
        f"# {marker}\n"
    )


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    """Return the bounded installed-CLI runner."""
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=420,
    )


def _assert_success(result: CommandResult) -> None:
    """Assert one command succeeded without leaking a configured credential."""
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    output = result.stdout + result.stderr
    assert all(secret not in output for secret in _SECRET_VALUES)


def _write_git_shim(
    shim_dir: Path,
    *,
    real_git: Path,
    record_path: Path,
) -> None:
    """Install a deterministic git shim that records auth at each transport call."""
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import hashlib

record_path = pathlib.Path(os.environ["APM_TEST_GIT_AUTH_RECORD"])
_GITHUB_CHILD_TOKEN_NAMES = (
    "COPILOT_GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GH_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_MODELS_KEY",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GITHUB_TOKEN",
)
argv = sys.argv[1:]
urls = []
for argument in argv:
    parsed = urllib.parse.urlsplit(argument)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        urls.append(
            {
                "scheme": parsed.scheme,
                "hostname": parsed.hostname,
                "port": parsed.port,
                "path": parsed.path,
                "username": parsed.username,
            }
        )
if urls:
    config_values = [
        value
        for key, value in os.environ.items()
        if key.startswith("GIT_CONFIG_VALUE_")
    ]
    record = {
        "argv0": argv[0] if argv else "",
        "argv_without_urls": [
            argument
            for argument in argv
            if not urllib.parse.urlsplit(argument).hostname
        ],
        "urls": urls,
        "git_token_sha256": (
            hashlib.sha256(os.environ["GIT_TOKEN"].encode()).hexdigest()
            if os.environ.get("GIT_TOKEN")
            else None
        ),
        "authorization_header_sha256": [
            hashlib.sha256(value.encode()).hexdigest()
            for value in config_values
            if value.lower().startswith("authorization:")
        ],
        "github_tokens_present": sorted(
            name
            for name in _GITHUB_CHILD_TOKEN_NAMES
            if os.environ.get(name)
        ),
        "git_config_global": os.environ.get("GIT_CONFIG_GLOBAL"),
        "git_config_nosystem": os.environ.get("GIT_CONFIG_NOSYSTEM"),
        "non_auth_git_config_keys": [
            os.environ.get(f"GIT_CONFIG_KEY_{index}", "")
            for index in range(int(os.environ.get("GIT_CONFIG_COUNT", "0")))
            if "extraheader"
            not in os.environ.get(f"GIT_CONFIG_KEY_{index}", "").lower()
        ],
    }
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")
    original = urllib.parse.urlsplit(os.environ["APM_TEST_ADO_SOURCE"])
    rewritten = os.environ["APM_TEST_LOCAL_REMOTE"]
    rewritten_argv = []
    for argument in argv:
        parsed = urllib.parse.urlsplit(argument)
        if (
            parsed.hostname == original.hostname
            and parsed.port == original.port
            and parsed.path.rstrip("/").removesuffix(".git")
            == original.path.rstrip("/").removesuffix(".git")
        ):
            rewritten_argv.append(rewritten)
        else:
            rewritten_argv.append(argument)
    argv = rewritten_argv
raise SystemExit(
    subprocess.run([os.environ["APM_TEST_REAL_GIT"], *argv], env=os.environ).returncode
)
""",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    assert real_git.is_file()


def _auth_records(record_path: Path) -> list[dict[str, object]]:
    """Return every transport-bearing git invocation captured by the shim."""
    return [
        json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines() if line
    ]


def _assert_ado_transport_isolation(records: list[dict[str, object]]) -> None:
    """Assert only the ADO PAT reached every on-prem ADO transport invocation."""
    assert records
    for record in records:
        urls = record["urls"]
        assert isinstance(urls, list)
        assert urls
        for url in urls:
            assert url == {
                "scheme": "https",
                "hostname": _ADO_HOST,
                "port": _ADO_PORT,
                "path": f"/{_ADO_ORGANIZATION}/{_ADO_PROJECT}/_git/{_ADO_REPOSITORY}",
                "username": None,
            }
        assert record["git_token_sha256"] is None
        encoded = base64.b64encode(f":{_ADO_PAT}".encode()).decode()
        expected_header = f"Authorization: Basic {encoded}"
        assert record["authorization_header_sha256"] == [
            hashlib.sha256(expected_header.encode()).hexdigest()
        ]
        assert record["github_tokens_present"] == [], record
        assert record["git_config_global"], record
        assert record["git_config_nosystem"] == "1"
        assert any(str(key).startswith("url.") for key in record["non_auth_git_config_keys"])


def _snapshot(project_root: Path) -> LifecycleStateSnapshot:
    """Capture the canonical durable lifecycle state for this consumer."""
    return LifecycleStateSnapshot.capture(project_root, targets=("copilot",))


def test_ado_server_overlap_install_update_audit_uses_only_ado_credential(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An overlapping GHES signal never escapes the canonical ADO auth route."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "ado-server-success",
        base_env=dict(os.environ),
    )
    fixture_env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    bundle = packages.create(_ADO_REPOSITORY, targets=("copilot",))
    source_skill = packages.add_skill(
        bundle,
        "ado-server-auth",
        _skill_document("version one"),
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=fixture_env)
    repository = repositories.create(_ADO_REPOSITORY, source_tree=bundle.root)
    first_commit = repositories.commit(repository, message="seed ADO Server auth package")

    repository_skill = repository.worktree / source_skill.relative_to(bundle.root)

    environment = repositories.url_rewrite_subprocess_env(repository, _SOURCE)
    record_path = isolated.temp_root / "git-auth.jsonl"
    resolved_git = shutil.which("git")
    assert resolved_git is not None
    real_git = Path(resolved_git)
    shim_dir = isolated.root / "bin"
    _write_git_shim(shim_dir, real_git=real_git, record_path=record_path)
    environment.update(
        {
            "PATH": f"{shim_dir}{os.pathsep}{environment['PATH']}",
            "APM_TEST_REAL_GIT": str(real_git),
            "APM_TEST_GIT_AUTH_RECORD": str(record_path),
            "APM_TEST_ADO_SOURCE": _SOURCE,
            "APM_TEST_LOCAL_REMOTE": repository.file_url,
            "GIT_EXEC_PATH": str(real_git.parent.parent / "libexec" / "git-core"),
            "ADO_HOST": _ADO_HOST,
            "GITHUB_HOST": _ADO_HOST,
            "ADO_APM_PAT": _ADO_PAT,
            "GITHUB_APM_PAT": _GITHUB_PAT,
            "GITHUB_TOKEN": _GITHUB_TOKEN,
            "GH_TOKEN": _GH_TOKEN,
            "APM_TIERED_RESOLVER": "0",
            "GIT_ALLOW_PROTOCOL": "https:file",
        }
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "ado-server-consumer",
        dependencies=(_DEPENDENCY,),
        targets=("copilot",),
    )
    runner = _runner(apm_binary_path)

    install = runner.run(
        _INSTALL_ARGS,
        scenario_id="ado-server-overlap-install",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(install)
    installed = _snapshot(consumer.root)
    locked = load_yaml(consumer.root / "apm.lock.yaml")["dependencies"][0]
    assert locked["host"] == _ADO_HOST
    assert locked["port"] == _ADO_PORT
    assert locked["repo_url"] == (f"{_ADO_ORGANIZATION}/{_ADO_PROJECT}/{_ADO_REPOSITORY}")
    assert locked["virtual_path"] == "skills/ado-server-auth"
    assert locked["resolved_commit"] == first_commit.sha
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("version one").encode()

    reinstall = runner.run(
        _INSTALL_ARGS,
        scenario_id="ado-server-overlap-reinstall",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(reinstall)
    reinstalled = _snapshot(consumer.root)
    assert reinstalled.semantic_bytes == installed.semantic_bytes

    repository_skill.write_bytes(_skill_document("version two").encode())
    second_commit = repositories.commit(repository, message="advance ADO Server auth package")
    update = runner.run(
        _UPDATE_ARGS,
        scenario_id="ado-server-overlap-update",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(update)
    updated = _snapshot(consumer.root)
    updated_lock = load_yaml(consumer.root / "apm.lock.yaml")["dependencies"][0]
    assert updated_lock["resolved_commit"] == second_commit.sha
    assert updated.semantic_bytes != installed.semantic_bytes
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("version two").encode()

    converged = runner.run(
        _UPDATE_ARGS,
        scenario_id="ado-server-overlap-update-converged",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(converged)
    assert _snapshot(consumer.root).semantic_bytes == updated.semantic_bytes

    audit = runner.run(
        _AUDIT_ARGS,
        scenario_id="ado-server-overlap-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(audit)
    audit_payload = json.loads(audit.stdout)
    assert audit_payload["passed"] is True
    assert audit_payload["summary"]["failed"] == 0

    records = _auth_records(record_path)
    _assert_ado_transport_isolation(records)
    serialized_records = record_path.read_text(encoding="utf-8")
    assert all(secret not in serialized_records for secret in _SECRET_VALUES)


def test_ado_server_auth_failure_is_secret_free_and_preserves_state(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A failed ADO Server update is diagnostic and commits no filesystem writes."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "ado-server-failure",
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()
    consumer = LocalPackageFactory(isolated.work_root).create(
        "ado-server-failure-consumer",
        dependencies=(_DEPENDENCY,),
        targets=("copilot",),
    )
    before = _snapshot(consumer.root)
    environment.update(
        {
            "ADO_HOST": _ADO_HOST,
            "GITHUB_HOST": _ADO_HOST,
            "GITHUB_APM_PAT": _GITHUB_PAT,
            "GITHUB_TOKEN": _GITHUB_TOKEN,
            "GH_TOKEN": _GH_TOKEN,
            "APM_NO_CACHE": "1",
            "APM_TIERED_RESOLVER": "0",
            "GIT_ALLOW_PROTOCOL": "https:file",
        }
    )

    result = _runner(apm_binary_path).run(
        _UPDATE_ARGS,
        scenario_id="ado-server-overlap-failure",
        cwd=consumer.root,
        env=environment,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Azure DevOps Server requires ADO_APM_PAT." in " ".join(output.split())
    assert "ADO_APM_PAT" in output
    assert "az login" not in output
    assert all(secret not in output for secret in _SECRET_VALUES)
    after = _snapshot(consumer.root)
    assert after.semantic_bytes == before.semantic_bytes
    assert after.manifest_bytes == before.manifest_bytes
    assert after.lockfile_bytes == before.lockfile_bytes
