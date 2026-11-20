"""Required packages use one host-aware identity on both sides (#3993).

A hostless ``require`` entry names the default host; a host-qualified entry
names that host. ``required-packages`` (manifest) and
``required-packages-deployed`` (lockfile) must agree on that identity, and
neither may skip an entry it cannot resolve.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import DependencyReference
from apm_cli.models.dependency.manifest_host import ManifestHostScope
from apm_cli.policy.policy_checks import (
    _check_required_executable_untrusted,
    _check_required_package_version,
    _check_required_packages,
    _check_required_packages_deployed,
)
from apm_cli.policy.schema import DependencyPolicy, ExecutablesPolicy

pytestmark = pytest.mark.unit

HOST = "gitlab.example.com"
QUALIFIED = f"{HOST}/group/sub/pkg"
HOSTLESS = "group/sub/pkg"


@pytest.fixture(autouse=True)
def _default_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HOST", raising=False)


def _lock(repo_url: str, host: str | None, **fields: str) -> LockFile:
    lock = LockFile()
    row = {"repo_url": repo_url, "resolved_ref": "main", **fields}
    if host is not None:
        row["host"] = host
    lock.add_dependency(LockedDependency.from_dict(row))
    return lock


def _both(deps, lock, entry: str):
    policy = DependencyPolicy(require=(entry,))
    return (
        _check_required_packages(deps, policy),
        _check_required_packages_deployed(deps, lock, policy),
    )


def test_reporter_repro_qualified_entry_passes_both_on_non_default_host() -> None:
    dep = DependencyReference.parse(QUALIFIED)
    lock = _lock(HOSTLESS, HOST, exec_status="deployed")
    assert list(lock.dependencies) == [QUALIFIED]

    manifest, deployed = _both([dep], lock, QUALIFIED)

    assert manifest.passed, manifest.message
    assert deployed.passed, deployed.message
    executables = _check_required_executable_untrusted(
        [dep], lock, ExecutablesPolicy(require=(QUALIFIED,))
    )
    assert executables.passed, executables.message


def test_reporter_repro_hostless_entry_fails_both_with_expected_form() -> None:
    dep = DependencyReference.parse(QUALIFIED)
    lock = _lock(HOSTLESS, HOST, exec_status="deployed")

    manifest, deployed = _both([dep], lock, HOSTLESS)

    for result in (manifest, deployed):
        assert not result.passed
        assert result.details == [HOSTLESS]
        assert f"'{QUALIFIED}'" in result.subjects[0].reason
        # `apm audit --ci` prints the message, not subject reasons. The
        # manifest fix comes first: the declared host may be a typosquat.
        assert f"declared on {HOST}, not github.com -- declare it on github.com" in result.message
        assert f"if {HOST} is trusted, change the policy require entry to '{QUALIFIED}'" in (
            result.message
        )
    assert manifest.subjects[0].remedy is None
    assert "absent from the lockfile" not in deployed.message
    executables = _check_required_executable_untrusted(
        [dep], lock, ExecutablesPolicy(require=(HOSTLESS,))
    )
    assert not executables.passed
    assert f"'{QUALIFIED}'" in executables.subjects[0].reason


def test_hostless_entry_keeps_working_on_default_host() -> None:
    dep = DependencyReference.parse("contoso/pkg")
    lock = _lock("contoso/pkg", "github.com")

    for entry in ("contoso/pkg", "Contoso/Pkg", "github.com/contoso/pkg"):
        manifest, deployed = _both([dep], lock, entry)
        assert manifest.passed, (entry, manifest.message)
        assert deployed.passed, (entry, deployed.message)


def test_hostless_entry_follows_configured_default_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example")
    dep = DependencyReference.parse("team/repo")
    lock = LockFile()
    lock.add_dependency(LockedDependency.from_dependency_ref(dep, None, 1, None))

    for entry in ("team/repo", "ghes.corp.example/team/repo"):
        manifest, deployed = _both([dep], lock, entry)
        assert manifest.passed, (entry, manifest.message)
        assert deployed.passed, (entry, deployed.message)


@pytest.mark.req("req-pl-018")
def test_qualified_entry_does_not_match_another_host() -> None:
    dep = DependencyReference.parse("contoso/pkg")
    lock = _lock("contoso/pkg", "github.com")

    manifest, deployed = _both([dep], lock, "gitlab.com/contoso/pkg")

    assert not manifest.passed
    assert not deployed.passed
    assert "'contoso/pkg'" in deployed.subjects[0].reason


@pytest.mark.parametrize("entry", [QUALIFIED, HOSTLESS])
def test_missing_package_fails_both_checks(entry: str) -> None:
    manifest, deployed = _both([], LockFile(), entry)

    assert not manifest.passed
    assert not deployed.passed
    assert manifest.details == deployed.details == [entry]


def test_declared_but_unlocked_qualified_entry_fails_deployed() -> None:
    dep = DependencyReference.parse(QUALIFIED)

    manifest, deployed = _both([dep], LockFile(), QUALIFIED)

    assert manifest.passed
    assert not deployed.passed
    assert "absent from the lockfile" in deployed.message


def test_version_pin_reads_the_host_qualified_lock_row() -> None:
    dep = DependencyReference.parse(f"{QUALIFIED}#v1.0.0")
    lock = _lock(HOSTLESS, HOST)
    policy = DependencyPolicy(require=(f"{QUALIFIED}#v1.0.0",), require_resolution="block")

    result = _check_required_package_version([dep], lock, policy)

    assert not result.passed
    assert result.details == [f"{QUALIFIED}: expected ref 'v1.0.0', got 'main'"]


def test_manifest_default_host_never_rebinds_a_hostless_entry() -> None:
    with ManifestHostScope("gitlab.corp.example"):
        dep = DependencyReference.parse("contoso/pkg")
    lock = LockFile()
    lock.add_dependency(LockedDependency.from_dependency_ref(dep, None, 1, None))

    hostless, _ = _both([dep], lock, "contoso/pkg")
    manifest, deployed = _both([dep], lock, "gitlab.corp.example/contoso/pkg")

    assert not hostless.passed
    assert manifest.passed, manifest.message
    assert deployed.passed, deployed.message


def test_proxy_shaped_reference_is_identified_by_its_written_host() -> None:
    dep = DependencyReference.parse("attacker.example/artifactory/x/acme/guardrails")

    for entry in ("acme/guardrails", "github.com/acme/guardrails"):
        assert not _check_required_packages([dep], DependencyPolicy(require=(entry,))).passed
    assert _check_required_packages(
        [dep], DependencyPolicy(require=("attacker.example/acme/guardrails",))
    ).passed


def test_version_pin_never_reads_a_same_path_row_from_another_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example")
    dep = DependencyReference.parse("github.com/owner/repo")
    lock = _lock("owner/repo", "github.com")
    policy = DependencyPolicy(require=("owner/repo#v9.9.9",), require_resolution="block")

    result = _check_required_package_version([dep], lock, policy)

    assert result.passed
    assert not result.subjects
