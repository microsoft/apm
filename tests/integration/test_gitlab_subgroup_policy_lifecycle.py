"""Installed-CLI GitLab namespace-ancestry policy composition (#2753).

The installed CLI discovers ``apm-policy`` projects along the project's real
GitLab origin namespace ancestry over HTTPS (a loopback proxy answers
``gitlab.com``), composes them company-to-team with the tighten-only merger,
and enforces the same effective policy in ``install`` and ``audit --ci``.
``policy status`` exposes the ordered, credential-redacted source chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_gitlab_policy_https_fixture import (
    LocalGitLabPolicyFixture,
    local_gitlab_policy_fixture,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_ORIGIN = "https://gitlab.com/acme/dept-a/team-x/service.git"
_COMPANY = "acme/apm-policy"
_DEPT = "acme/dept-a/apm-policy"
_TEAM = "acme/dept-a/team-x/apm-policy"
_PROBED = [_TEAM, _DEPT, _COMPANY]
_DENIED = "evil-org/evil-pkg"


def _policy(name: str, *, deny: str | None = None, enforcement: str = "block") -> str:
    text = f"name: {name}\nenforcement: {enforcement}\n"
    if deny is not None:
        text += f"dependencies:\n  deny: [{deny}]\n"
    return text


_COMPANY_POLICY = _policy("company", deny=_DENIED)
_TEAM_POLICY = _policy("team", deny="team-only/blocked", enforcement="warn")


class _Scenario:
    """One isolated GitLab-hosted consumer project driven by the installed CLI."""

    def __init__(
        self, tmp_path: Path, apm_binary_path: Path, name: str, *, pin: str | None = None
    ) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / name, base_env=dict(os.environ))
        self.project = self.isolated.work_root / "service"
        self.project.mkdir()
        manifest = (
            "name: service\nversion: 1.0.0\ndescription: governed\n"
            f"targets:\n  - copilot\ndependencies:\n  apm:\n    - {_DENIED}\n"
        )
        if pin is not None:
            manifest += f"policy:\n  hash: {pin}\n"
        (self.project / "apm.yml").write_text(manifest, encoding="utf-8")
        env = self.isolated.subprocess_env()
        for args in (("init", "--quiet"), ("remote", "add", "origin", _ORIGIN)):
            subprocess.run(
                ["git", *args], cwd=self.project, env=env, check=True, capture_output=True
            )
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240)

    def run(self, fixture: LocalGitLabPolicyFixture, *args: str, scenario: str) -> CommandResult:
        env = fixture.child_environment(self.isolated.subprocess_env())
        return self.runner.run(args, scenario_id=scenario, cwd=self.project, env=env)

    def install(self, fixture: LocalGitLabPolicyFixture) -> CommandResult:
        return self.run(fixture, "install", "--target", "copilot", scenario="install")

    def audit(self, fixture: LocalGitLabPolicyFixture) -> CommandResult:
        return self.run(
            fixture,
            *("audit", "--ci", "--no-fail-fast", "--no-drift", "-f", "json"),
            scenario="audit",
        )

    def expire_policy_cache(self) -> None:
        shutil.rmtree(self.isolated.cache_root / "policy_v1", ignore_errors=True)

    def status(self, fixture: LocalGitLabPolicyFixture, *extra: str) -> dict:
        result = self.run(fixture, "policy", "status", "--json", *extra, scenario="status")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


def _probed(fixture: LocalGitLabPolicyFixture) -> list[str]:
    return [request.project for request in fixture.requests]


def _assert_install_denies(result: CommandResult) -> None:
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Install blocked by org policy" in output, output
    assert f"{_DENIED}: denied by pattern: {_DENIED}" in output, output


def _denylist_check(report: dict) -> dict:
    return next(check for check in report["checks"] if check["name"] == "dependency-denylist")


def _pin(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_WEAKER_TEAM = f"name: team\nenforcement: off\ndependencies:\n  allow: [{_DENIED}]\n"

_HIERARCHIES = {
    "company-only": ({_COMPANY: _COMPANY_POLICY}, [_COMPANY]),
    "team-only": ({_TEAM: _policy("team", deny=_DENIED)}, [_TEAM]),
    "company-plus-team-missing-intermediate": (
        {_COMPANY: _COMPANY_POLICY, _TEAM: _TEAM_POLICY},
        [_COMPANY, _TEAM],
    ),
    "multiple-subgroup-levels": (
        {
            _COMPANY: _COMPANY_POLICY,
            _DEPT: _policy("dept", deny="dept/blocked"),
            _TEAM: _TEAM_POLICY,
        },
        [_COMPANY, _DEPT, _TEAM],
    ),
    "empty-child": ({_COMPANY: _COMPANY_POLICY, _TEAM: "name: team\n"}, [_COMPANY, _TEAM]),
    "extends-plus-ancestry": (
        {
            _COMPANY: _COMPANY_POLICY,
            _DEPT: _policy("dept", deny="dept/blocked") + "extends: org\n",
            _TEAM: _TEAM_POLICY + "extends: org\n",
        },
        [_COMPANY, _DEPT, _TEAM],
    ),
    "weaker-child": ({_COMPANY: _COMPANY_POLICY, _TEAM: _WEAKER_TEAM}, [_COMPANY, _TEAM]),
}


@pytest.mark.parametrize("hierarchy", sorted(_HIERARCHIES))
def test_install_and_audit_enforce_the_composed_ancestry(
    tmp_path: Path, apm_binary_path: Path, hierarchy: str
) -> None:
    """Install and audit apply the same company-to-team chain; no ancestor rule is lost."""
    projects, chain = _HIERARCHIES[hierarchy]
    scenario = _Scenario(tmp_path, apm_binary_path, hierarchy)
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        fixture.publish(projects)
        status = scenario.status(fixture, "--no-cache")
        probed = _probed(fixture)
        install = scenario.install(fixture)
        audit = scenario.audit(fixture)

    report = json.loads(audit.stdout)
    assert probed == _PROBED
    assert status["outcome"] == "found"
    assert status["source"] == f"org:gitlab.com/{chain[-1]}"
    assert status["extends_chain"] == [f"gitlab.com/{ref}" for ref in chain[:-1]]
    _assert_install_denies(install)
    assert audit.returncode == 1, audit.stdout
    assert report["policy"]["source"] == f"org:gitlab.com/{chain[-1]}"
    assert report["policy"]["chain"] == [f"gitlab.com/{ref}" for ref in chain]
    denylist = _denylist_check(report)
    assert denylist["passed"] is False
    assert any(_DENIED in detail for detail in denylist["details"])


def test_no_policy_in_any_namespace_is_clean_absence(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _Scenario(tmp_path, apm_binary_path, "absent")
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        fixture.publish({})
        status = scenario.status(fixture, "--no-cache")
        probed = _probed(fixture)
        dry_run = scenario.run(
            fixture, "install", "--dry-run", "--target", "copilot", scenario="dry-run"
        )

    assert probed == _PROBED
    assert status["outcome"] == "absent"
    assert status["error"] is None
    assert dry_run.returncode == 0, dry_run.stdout
    assert "Would be blocked" not in dry_run.stdout


@pytest.mark.parametrize("failure", [403, 500], ids=["access-denied", "server-error"])
def test_ancestor_failure_never_applies_a_weaker_partial_chain(
    tmp_path: Path, apm_binary_path: Path, failure: int
) -> None:
    """A non-404 ancestor failure fails closed even when the team policy is warn-only."""
    scenario = _Scenario(tmp_path, apm_binary_path, f"failure-{failure}")
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        fixture.publish({_COMPANY: failure, _TEAM: _TEAM_POLICY})
        status = scenario.status(fixture, "--no-cache")
        install = scenario.install(fixture)
        audit = scenario.audit(fixture)

    assert status["outcome"] == "incomplete_chain"
    assert "gitlab.com/acme/apm-policy" in status["error"]
    assert install.returncode == 1, install.stdout
    assert "inheritance chain is incomplete" in install.stdout + install.stderr
    assert audit.returncode == 1, audit.stdout
    assert "Policy chain incomplete" in audit.stderr, audit.stderr


def test_pin_follows_the_nearest_policy_and_never_silently_repins(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    dept_policy = _policy("dept", deny="dept/blocked", enforcement="warn")
    scenario = _Scenario(tmp_path, apm_binary_path, "pin", pin=_pin(dept_policy))
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        fixture.publish({_COMPANY: _COMPANY_POLICY, _DEPT: dept_policy})
        pinned = scenario.install(fixture)
        scenario.expire_policy_cache()
        fixture.publish({_COMPANY: _COMPANY_POLICY, _DEPT: dept_policy, _TEAM: _TEAM_POLICY})
        closer = scenario.install(fixture)
        status = scenario.status(fixture, "--no-cache")

    _assert_install_denies(pinned)
    assert closer.returncode == 1, closer.stdout
    assert "policy hash mismatch" in closer.stdout + closer.stderr
    assert status["outcome"] == "hash_mismatch"
    assert "hash:" in (scenario.project / "apm.yml").read_text(encoding="utf-8")
    assert _pin(dept_policy) in (scenario.project / "apm.yml").read_text(encoding="utf-8")


def test_cached_composition_enforces_offline(tmp_path: Path, apm_binary_path: Path) -> None:
    """A complete cached chain keeps every ancestor rule without contacting GitLab."""
    scenario = _Scenario(tmp_path, apm_binary_path, "offline")
    with local_gitlab_policy_fixture(tmp_path / "certs") as fixture:
        fixture.publish({_COMPANY: _COMPANY_POLICY, _TEAM: _TEAM_POLICY})
        online = scenario.install(fixture)
        fixture.publish({})
        cached = scenario.install(fixture)
        cached_requests = _probed(fixture)

    _assert_install_denies(online)
    _assert_install_denies(cached)
    assert cached_requests == []
    assert "(extends chain: gitlab.com/acme/apm-policy)" in " ".join(cached.stdout.split())
