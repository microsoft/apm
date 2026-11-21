"""Org policy hosted on Azure DevOps is fetched through the credential helper (#3758).

When the policy repository (``<org>/apm/apm-policy``) answers an anonymous
Items API request with a 302 to the ``*.vssps.visualstudio.com/_signin``
page, discovery must treat that as "authentication required" and continue
the credential chain (PAT, Azure CLI, then the path-scoped Git credential
helper) instead of reporting a fetch failure. A fetch failure must never be a
silent pass: ``policy.fetch_failure_default: block`` fails closed for
install, ``audit --ci`` and lifecycle scripts, and ``warn`` names the cause.

Cross-track (targets, T2 brief section 6 L1-L3): once the ADO-hosted policy
is fetched, the install gate and ``audit --ci`` evaluate the canonical
project targets (plural ``targets:``, CSV, aliases) against
``compilation.target.allow``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._ado_auth_kit import (
    HELPER_SECRET,
    ORG,
    AdoAuthScenario,
    describe,
    flat,
)
from tests.utils.local_ado_org_fixture import ADO_HOST
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import basic_authorization

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX credential helper launcher"),
]

_POLICY_HELPER_PATH = f"{ORG}/apm/_git/apm-policy"
_PAT = "ado-policy-pat-sentinel-3c71"
_WARN_POLICY = (
    "name: contoso-org\nversion: '1.0'\nenforcement: warn\n"
    "dependencies:\n  deny: [untrusted/package]\n"
)
_TARGET_POLICY = (
    "name: contoso-org\nversion: '1.0'\nenforcement: block\n"
    "compilation:\n  target:\n    allow: [claude, copilot]\n"
)
_GEN_SCRIPT = '#!/bin/sh\ncat > /dev/null\necho ran >> "$PWD/ran.log"\n'


def _publish_policy(scenario: AdoAuthScenario, text: str) -> None:
    root = scenario.isolated.package_root / "apm-policy"
    root.mkdir(parents=True)
    (root / "apm-policy.yml").write_text(text, encoding="utf-8")
    repository = scenario.repositories.create("apm-policy", source_tree=root)
    scenario.repositories.commit(repository, message="org policy")
    scenario.ado.add_repository(ORG, "apm", "apm-policy", repository.origin)


def _ado_project(scenario: AdoAuthScenario, name: str, manifest_text: str) -> Path:
    project = scenario.project(name, manifest=manifest_text)
    env = scenario.isolated.subprocess_env()
    for args in (
        ("git", "init", "--quiet", "--initial-branch=main"),
        ("git", "remote", "add", "origin", f"https://{ADO_HOST}/{ORG}/application/_git/service"),
    ):
        subprocess.run(args, cwd=project, env=env, check=True, capture_output=True, timeout=30)
    return project


def _local_package(project: Path) -> str:
    package = project / "pkg"
    (package / ".apm" / "instructions").mkdir(parents=True)
    (package / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    (package / ".apm" / "instructions" / "pkg.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nLocal rule.\n', encoding="utf-8"
    )
    return "    - ./pkg\n"


def _policy_status(scenario: AdoAuthScenario, project: Path, env: dict[str, str]) -> dict:
    result = scenario.run(
        ("policy", "status", "--no-cache", "--json"), cwd=project, env=env, step="policy-status"
    )
    assert result.returncode == 0, describe(result)
    start = result.stdout.find("{")
    return json.loads(result.stdout[start : result.stdout.rfind("}") + 1])


def _assert_policy_helper_scoped(scenario: AdoAuthScenario) -> None:
    gets = scenario.helper.gets() if scenario.helper else ()
    assert gets, "credential helper was never asked for the policy repository"
    for event in gets:
        assert event["fields"].get("host") == ADO_HOST, event
        assert event["fields"].get("path", "").removesuffix(".git") in (
            _POLICY_HELPER_PATH,
            f"{ORG}/apm/apm-policy",
            f"{ORG}/_apm/_git/_apm",
            f"{ORG}/_apm/_apm",
        ), event


def test_policy_status_follows_signin_redirect_into_credential_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``policy status --no-cache`` reports ``found`` when only the helper can read the policy."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    _publish_policy(scenario, _WARN_POLICY)
    project = _ado_project(scenario, "status", "name: consumer\nversion: 1.0.0\n")
    with scenario.serve() as proxy:
        report = _policy_status(scenario, project, scenario.env(proxy))
        evidence = trace_text(proxy)
        to_signin = [o for o in proxy.observations if o.host != ADO_HOST and o.method != "CONNECT"]
    assert report["outcome"] == "found", (report, evidence)
    assert HELPER_SECRET not in json.dumps(report)
    assert to_signin == [], "credentials or requests followed the sign-in redirect"
    _assert_policy_helper_scoped(scenario)


def test_policy_signin_redirect_without_helper_is_reported_as_auth_required(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """With no credential anywhere, the report names authentication, not a raw redirect."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper(respond=False)
    _publish_policy(scenario, _WARN_POLICY)
    project = _ado_project(scenario, "status-none", "name: consumer\nversion: 1.0.0\n")
    with scenario.serve() as proxy:
        report = _policy_status(scenario, project, scenario.env(proxy))
    assert report["outcome"] != "found"
    text = json.dumps(report)
    assert "Refusing HTTP redirect" not in text, text
    assert "ADO_APM_PAT" in text or "credential" in text.lower(), text
    assert scenario.helper is not None and scenario.helper.gets(), "helper was not consulted"


@pytest.mark.req("req-pl-010")
def test_audit_ci_and_install_fail_closed_when_ado_policy_unreachable_under_block(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Regression lock: fetch failure + ``fetch_failure_default: block`` never passes silently."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper(respond=False)
    _publish_policy(scenario, _TARGET_POLICY)
    project = _ado_project(scenario, "block", "")
    deps = _local_package(project)
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ntargets: [copilot]\n"
        f"dependencies:\n  apm:\n{deps}policy:\n  fetch_failure_default: block\n",
        encoding="utf-8",
    )
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        seed = scenario.run(
            ("install", "--no-policy", "--target", "copilot"), cwd=project, env=env, step="seed"
        )
        assert seed.returncode == 0, describe(seed)
        lock_before = (project / "apm.lock.yaml").read_bytes()
        install = scenario.run(
            ("install", "--target", "copilot"), cwd=project, env=env, step="install-block"
        )
        audit = scenario.run(("audit", "--ci"), cwd=project, env=env, step="audit-block")
    assert install.returncode != 0, describe(install)
    assert (project / "apm.lock.yaml").read_bytes() == lock_before
    assert audit.returncode != 0, describe(audit)
    assert "fetch_failure_default=block" in flat(audit), describe(audit)


def test_audit_ci_warn_names_unreachable_ado_policy(tmp_path: Path, apm_binary_path: Path) -> None:
    """Regression lock: under ``warn`` the skipped enforcement is announced, never silent."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper(respond=False)
    _publish_policy(scenario, _TARGET_POLICY)
    project = _ado_project(
        scenario, "warn", "name: consumer\nversion: 1.0.0\ntargets: [claude, cursor]\n"
    )
    with scenario.serve() as proxy:
        audit = scenario.run(("audit", "--ci"), cwd=project, env=scenario.env(proxy), step="warn")
    text = flat(audit)
    assert "enforcement skipped" in text, describe(audit)


@pytest.mark.parametrize(
    ("targets_line", "expect_blocked"),
    [
        ("targets: [claude, cursor]", True),
        ('target: "claude,copilot"', False),
        ("targets: [vscode]", False),
    ],
    ids=["plural-noncompliant", "csv-compliant", "alias-compliant"],
)
@pytest.mark.parametrize("credential", ["helper", "pat"])
def test_ado_hosted_target_policy_is_enforced_on_canonical_targets(
    tmp_path: Path, apm_binary_path: Path, targets_line: str, expect_blocked: bool, credential: str
) -> None:
    """Install gate and ``audit --ci`` agree on canonical targets for an ADO-hosted policy.

    ``pat`` isolates the targets semantics (policy reachable on every build);
    ``helper`` additionally needs the credential-helper policy fetch.
    """
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    overrides: dict[str, str] = {}
    if credential == "helper":
        scenario.install_helper()
    else:
        scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
        overrides["ADO_APM_PAT"] = _PAT
    _publish_policy(scenario, _TARGET_POLICY)
    project = _ado_project(scenario, "targets", "")
    deps = _local_package(project)
    (project / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\n{targets_line}\n"
        f"dependencies:\n  apm:\n{deps}policy:\n  fetch_failure_default: block\n",
        encoding="utf-8",
    )
    manifest_before = (project / "apm.yml").read_bytes()
    with scenario.serve() as proxy:
        env = scenario.env(proxy, overrides)
        status = _policy_status(scenario, project, env)
        seed = scenario.run(("install", "--no-policy"), cwd=project, env=env, step="seed")
        assert seed.returncode == 0, describe(seed)
        seeded_lock = (project / "apm.lock.yaml").read_bytes()
        install = scenario.run(("install",), cwd=project, env=env, step="install-targets")
        lock_after_install = (
            (project / "apm.lock.yaml").read_bytes()
            if (project / "apm.lock.yaml").exists()
            else None
        )
        audit = scenario.run(("audit", "--ci"), cwd=project, env=env, step="audit-targets")
    assert status["outcome"] == "found", status
    assert "fetch_failure_default=block" not in flat(audit), describe(audit)
    if expect_blocked:
        assert install.returncode != 0, describe(install)
        assert lock_after_install == seeded_lock, "blocked install rewrote the lockfile"
        assert audit.returncode != 0, describe(audit)
        assert "compilation-target" in flat(audit) or "not in allowed" in flat(audit), describe(
            audit
        )
    else:
        assert install.returncode == 0, describe(install)
        assert audit.returncode == 0, describe(audit)
    assert (project / "apm.yml").read_bytes() == manifest_before
    current_lock = (
        (project / "apm.lock.yaml").read_bytes() if (project / "apm.lock.yaml").exists() else None
    )
    assert current_lock == lock_after_install, "audit rewrote the lockfile"


def _lifecycle_project(scenario: AdoAuthScenario, name: str, event: str) -> Path:
    project = _ado_project(scenario, name, "")
    deps = _local_package(project)
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ntargets: [copilot]\n"
        f"dependencies:\n  apm:\n{deps}"
        "policy:\n  fetch_failure_default: block\n"
        f'lifecycle:\n  {event}:\n    - type: command\n      run: "sh ./gen.sh"\n',
        encoding="utf-8",
    )
    (project / "gen.sh").write_text(_GEN_SCRIPT, encoding="utf-8")
    (project / ".apm" / "instructions").mkdir(parents=True)
    (project / ".apm" / "instructions" / "own.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nProject rule.\n', encoding="utf-8"
    )
    return project


def _ran(project: Path) -> int:
    log = project / "ran.log"
    return len(log.read_text(encoding="ascii").splitlines()) if log.exists() else 0


def test_compile_lifecycle_runs_when_ado_policy_needs_credential_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A trusted ``pre-compile`` script is not suppressed when the helper can read the policy."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    _publish_policy(scenario, _WARN_POLICY)
    project = _lifecycle_project(scenario, "compile", "pre-compile")
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        trust = scenario.run(("lifecycle", "trust"), cwd=project, env=env, step="trust")
        assert trust.returncode == 0, describe(trust)
        compile_result = scenario.run(("compile",), cwd=project, env=env, step="compile")
    assert compile_result.returncode == 0, describe(compile_result)
    assert "suppressed" not in flat(compile_result), describe(compile_result)
    assert _ran(project) == 1, describe(compile_result)


def test_uninstall_lifecycle_runs_when_ado_policy_needs_credential_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A trusted ``pre-uninstall`` script is not suppressed on a cold policy cache."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    _publish_policy(scenario, _WARN_POLICY)
    project = _lifecycle_project(scenario, "uninstall", "pre-uninstall")
    with scenario.serve() as proxy:
        env = scenario.env(proxy)
        install = scenario.run(
            ("install", "--no-policy", "--target", "copilot"), cwd=project, env=env, step="seed"
        )
        assert install.returncode == 0, describe(install)
        trust = scenario.run(("lifecycle", "trust"), cwd=project, env=env, step="trust")
        assert trust.returncode == 0, describe(trust)
        uninstall = scenario.run(("uninstall", "./pkg"), cwd=project, env=env, step="uninstall")
    assert uninstall.returncode == 0, describe(uninstall)
    assert "suppressed" not in flat(uninstall), describe(uninstall)
    assert _ran(project) == 1, describe(uninstall)


def test_warm_policy_cache_does_not_mask_refresh_through_helper(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A cache warmed with a PAT keeps working, and a forced refresh still succeeds via the helper."""
    scenario = AdoAuthScenario(tmp_path, apm_binary_path)
    scenario.install_helper()
    scenario.ado.accepted_authorizations.add(basic_authorization("", _PAT))
    _publish_policy(scenario, _WARN_POLICY)
    project = _ado_project(scenario, "warm", "name: consumer\nversion: 1.0.0\n")
    with scenario.serve() as proxy:
        warm = scenario.run(
            ("policy", "status", "--json"),
            cwd=project,
            env=scenario.env(proxy, {"ADO_APM_PAT": _PAT}),
            step="warm",
        )
        assert warm.returncode == 0, describe(warm)
        assert '"outcome": "found"' in warm.stdout, describe(warm)
        proxy.clear()
        cached = scenario.run(
            ("policy", "status", "--json"), cwd=project, env=scenario.env(proxy), step="cached"
        )
        assert '"outcome": "found"' in cached.stdout, describe(cached)
        assert proxy.requests_to(ADO_HOST) == (), "warm cache was not served offline"
        refreshed = _policy_status(scenario, project, scenario.env(proxy))
    assert refreshed["outcome"] == "found", refreshed
    assert refreshed["cached"] is False, refreshed


def trace_text(proxy) -> str:
    return "\n".join(
        f"{o.host} {o.method} {o.target[:100]} -> {o.status}" for o in proxy.observations
    )
