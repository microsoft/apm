"""Registry and GitLab credentials stay on the origin they are configured for (#3649).

Promises (docs/enterprise/registry-proxy.md, getting-started/authentication.md):
``PROXY_REGISTRY_TOKEN`` is the bearer for the configured ``PROXY_REGISTRY_URL``,
and every token-bearing request goes only to its verified host. Chains drive the
installed CLI through a recording HTTPS proxy so each origin's headers are
observed directly:

* an explicit-FQDN Artifactory dependency on another origin (declared by a
  dependency package) receives no registry token, while the configured
  registry origin still does;
* a GitLab file download keeps ``PRIVATE-TOKEN`` on the GitLab origin when
  that origin answers with a redirect to another host.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_header_capture_proxy import local_header_capture_proxy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
]

REGISTRY_HOST = "art.example.test"
OTHER_HOST = "other.example.test"
GITLAB_HOST = "gitlab.example.test"
REGISTRY_SECRET = "registry-origin-sentinel-0001"
GITLAB_SECRET = "gitlab-origin-sentinel-0002"


class _Project:
    """One consumer project driven by the installed CLI."""

    def __init__(self, root: Path, binary: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(root / "scenario", base_env=dict(os.environ))
        self.project = self.isolated.work_root / "consumer"
        (self.project / ".claude").mkdir(parents=True)
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)

    def write(self, relative: str, text: str) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def install(self, env: dict[str, str]) -> CommandResult:
        return self.runner.run(
            ("install", "--no-policy", "--parallel-downloads", "0"),
            cwd=self.project,
            env=env,
            scenario_id="credential-origin",
        )


def _manifest(name: str, dependency: str) -> str:
    return f"name: {name}\nversion: 1.0.0\ndependencies:\n  apm:\n    - {dependency}\n"


def _registry_env(project: _Project, proxy) -> dict[str, str]:
    env = proxy.child_environment(project.isolated.subprocess_env())
    env["APM_NON_INTERACTIVE"] = "1"
    env["PROXY_REGISTRY_URL"] = f"https://{REGISTRY_HOST}/artifactory/github"
    env["PROXY_REGISTRY_TOKEN"] = REGISTRY_SECRET
    return env


def test_dependency_declared_registry_fqdn_on_other_origin_gets_no_registry_token(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.write("pkg/apm.yml", _manifest("pkg", f"{OTHER_HOST}/artifactory/github/octo/repo"))
    project.write("apm.yml", _manifest("consumer", "./pkg"))

    with local_header_capture_proxy(tmp_path / "tls", (REGISTRY_HOST, OTHER_HOST)) as proxy:
        project.install(_registry_env(project, proxy))

    assert any(o.host == OTHER_HOST for o in proxy.observations), proxy.observations
    assert proxy.hosts_receiving(REGISTRY_SECRET) == set()


def test_configured_registry_origin_still_receives_registry_token(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.write("apm.yml", _manifest("consumer", f"{REGISTRY_HOST}/artifactory/github/octo/repo"))

    with local_header_capture_proxy(tmp_path / "tls", (REGISTRY_HOST, OTHER_HOST)) as proxy:
        project.install(_registry_env(project, proxy))

    assert proxy.hosts_receiving(REGISTRY_SECRET) == {REGISTRY_HOST}


def test_gitlab_file_token_stays_on_gitlab_origin_across_redirect(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.write(
        "apm.yml", _manifest("consumer", f"{GITLAB_HOST}/grp/proj/prompts/review.prompt.md")
    )

    with local_header_capture_proxy(
        tmp_path / "tls",
        (GITLAB_HOST, OTHER_HOST),
        redirects={GITLAB_HOST: f"https://{OTHER_HOST}/landing"},
    ) as proxy:
        env = proxy.child_environment(project.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        env["GITLAB_HOST"] = GITLAB_HOST
        env["GITLAB_APM_PAT"] = GITLAB_SECRET
        project.install(env)

    assert any(o.host == OTHER_HOST for o in proxy.observations), proxy.observations
    assert proxy.hosts_receiving(GITLAB_SECRET) == {GITLAB_HOST}
