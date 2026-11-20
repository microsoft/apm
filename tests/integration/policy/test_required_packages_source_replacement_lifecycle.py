"""Lifecycle: required-package matching follows source replacement (#4038).

A consumer declares ``example-org/app`` and a root ``replace:`` selects the
fork ``fork-org/app-fork`` (on github.com, or on a non-default host). The org
policy (``enforcement: block``) is discovered from the project's ``origin``
remote and served by a loopback HTTPS proxy; packages come from local bare
repositories behind git URL rewrites, so nothing leaves the machine.

``apm install`` (gate) and ``apm audit --ci`` must agree under req-rpl-002:
a ``dependencies.require`` entry identifies the selected package, and the
lock entry is read under the original's lockfile identity. Without
``replace:`` the declared package keeps matching.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_package import LocalPackageFactory
from tests.utils.local_scripted_https_proxy import (
    ScriptedRequest,
    ScriptedResponse,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

ORIGINAL = "example-org/app"
FORK = "fork-org/app-fork"
GHES = "ghes.example.test"
SCHEMA = "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
POLICY_PATH = "/repos/contoso/.github/contents/apm-policy.yml"
PRESENCE = ("required-packages", "required-packages-deployed")


@dataclass
class _Outcome:
    install: CommandResult
    audit: CommandResult
    lock: dict

    @property
    def install_output(self) -> str:
        return " ".join((self.install.stdout + self.install.stderr).split())

    @property
    def checks(self) -> dict[str, dict]:
        return {check["name"]: check for check in json.loads(self.audit.stdout)["checks"]}


def _scenario(
    tmp_path: Path,
    apm_binary_path: Path,
    entry: str,
    *,
    fork_host: str | None = "github.com",
    **policy: object,
) -> _Outcome:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env={})
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    served = {}
    for name in ("app", "app-fork"):
        bundle = packages.create(name, targets=("copilot",))
        packages.add_skill(
            bundle, f"{name}-skill", f"---\nname: {name}-skill\ndescription: x\n---\n# {name}\n"
        )
        repository = repositories.create(name, source_tree=bundle.root)
        repositories.tag(repository, "v1.0.0", repositories.commit(repository, message="v1"))
        served[name] = repository
    fork_url = f"https://{fork_host or 'github.com'}/{FORK}"
    env = repositories.url_rewrite_subprocess_env_many(
        (
            (served["app"], f"https://github.com/{ORIGINAL}.git"),
            (served["app-fork"], f"{fork_url}.git"),
        )
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest: dict = {
        "name": "consumer",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": [f"{ORIGINAL}#v1.0.0"]},
    }
    if fork_host is not None:
        manifest = {
            "$schema": SCHEMA,
            **manifest,
            "replace": [
                {
                    "for": {"git": f"https://github.com/{ORIGINAL}"},
                    "with": {"git": fork_url, "ref": "v1.0.0"},
                }
            ],
        }
    dump_yaml(manifest, project / "apm.yml")
    for args in (
        ("init", "-q"),
        ("remote", "add", "origin", "https://github.com/contoso/consumer.git"),
    ):
        subprocess.run(("git", *args), cwd=project, env=env, check=True)

    document = {
        "name": "require-replacement",
        "version": "1.0.0",
        "enforcement": "block",
        "dependencies": {"require": [entry], **policy},
    }
    body = json.dumps(
        {
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(document).encode()).decode(),
        }
    ).encode()

    def github_api(request: ScriptedRequest) -> ScriptedResponse:
        if request.path == POLICY_PATH:
            return ScriptedResponse(200, {"Content-Type": "application/json"}, body)
        return ScriptedResponse(404)

    runner = ApmLifecycleRunner((str(apm_binary_path),))
    with local_scripted_https_proxy(isolated.root / "tls", {"api.github.com": github_api}) as px:
        child = px.child_environment(env)
        child["APM_NON_INTERACTIVE"] = "1"

        def apm(*args: str) -> CommandResult:
            return runner.run(args, scenario_id="-".join(args[:2]), cwd=project, env=child)

        installed = apm("install", "--target", "copilot")
        if installed.returncode != 0:
            repaired = apm("install", "--target", "copilot", "--no-policy")
            assert repaired.returncode == 0, repaired.stdout + repaired.stderr
        audited = apm("audit", "--ci", "--no-fail-fast", "--format", "json")
        assert px.requests_to("api.github.com"), "org policy was not discovered"
    return _Outcome(installed, audited, load_yaml(project / "apm.lock.yaml") or {})


def _locked_original(outcome: _Outcome, fork_url: str) -> None:
    rows = outcome.lock.get("dependencies", [])
    assert [(row["repo_url"], row["replacement"]["source"]["git"]) for row in rows] == [
        (ORIGINAL, fork_url)
    ]


@pytest.mark.parametrize(
    ("fork_host", "entry"),
    [("github.com", ORIGINAL), (GHES, ORIGINAL), (GHES, f"github.com/{ORIGINAL}")],
    ids=["github-fork", "ghes-fork", "ghes-fork-qualified-original"],
)
def test_original_entry_does_not_identify_a_replaced_dependency(
    tmp_path: Path, apm_binary_path: Path, fork_host: str, entry: str
) -> None:
    outcome = _scenario(tmp_path, apm_binary_path, entry, fork_host=fork_host)

    assert outcome.install.returncode != 0
    assert "(required-packages)" in outcome.install_output
    _locked_original(outcome, f"https://{fork_host}/{FORK}")
    assert outcome.audit.returncode != 0
    for name in PRESENCE:
        assert not outcome.checks[name]["passed"], outcome.checks[name]
        assert outcome.checks[name]["details"] == [entry]


@pytest.mark.parametrize(
    ("fork_host", "entry"),
    [("github.com", FORK), (GHES, f"{GHES}/{FORK}")],
    ids=["github-fork", "ghes-fork"],
)
def test_selected_entry_identifies_the_replaced_dependency(
    tmp_path: Path, apm_binary_path: Path, fork_host: str, entry: str
) -> None:
    outcome = _scenario(tmp_path, apm_binary_path, entry, fork_host=fork_host)

    assert outcome.install.returncode == 0, outcome.install_output
    _locked_original(outcome, f"https://{fork_host}/{FORK}")
    assert outcome.audit.returncode == 0, outcome.audit.stdout
    for name in PRESENCE:
        assert outcome.checks[name]["passed"], outcome.checks[name]


def test_selected_entry_version_pin_reads_the_locked_ref(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    outcome = _scenario(tmp_path, apm_binary_path, f"{FORK}#v9.9.9", require_resolution="block")
    # A fresh install has no locked ref to compare; the gate admits the selected entry.
    assert outcome.install.returncode == 0, outcome.install_output

    check = outcome.checks["required-package-version"]
    assert not check["passed"], check
    assert check["details"] == [f"{FORK}: expected ref 'v9.9.9', got 'v1.0.0'"]
    for name in PRESENCE:
        assert outcome.checks[name]["passed"], outcome.checks[name]


def test_without_replacement_the_declared_package_keeps_matching(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    outcome = _scenario(tmp_path, apm_binary_path, ORIGINAL, fork_host=None)

    assert outcome.install.returncode == 0, outcome.install_output
    assert outcome.audit.returncode == 0, outcome.audit.stdout
    for name in PRESENCE:
        assert outcome.checks[name]["passed"], outcome.checks[name]
