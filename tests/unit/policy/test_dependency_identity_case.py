"""Security regressions for dependency-policy identity casing."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.identity import normalize_package_policy_identity
from apm_cli.policy.matcher import check_mcp_allowed, matches_pattern
from apm_cli.policy.policy_checks import (
    _check_dependency_allowlist,
    _check_dependency_denylist,
    _check_registry_source,
    _check_required_executable_untrusted,
    _check_required_package_version,
    _check_required_packages,
    _check_required_packages_deployed,
)
from apm_cli.policy.schema import (
    DependencyPolicy,
    ExecutablesPolicy,
    McpPolicy,
    RegistrySourcePolicy,
)


def _lock_dependency(
    repo_url: str,
    *,
    resolved_ref: str | None = None,
    exec_status: str | None = None,
) -> LockFile:
    lock = LockFile()
    data: dict[str, str] = {"repo_url": repo_url}
    if resolved_ref is not None:
        data["resolved_ref"] = resolved_ref
    if exec_status is not None:
        data["exec_status"] = exec_status
    lock.add_dependency(LockedDependency.from_dict(data))
    return lock


@pytest.mark.parametrize(
    ("dependency", "pattern"),
    [
        ("DevExpGbb/Secure-Baseline", "devexpgbb/secure-baseline"),
        ("devexpgbb/secure-baseline", "DevExpGbb/Secure-Baseline"),
        ("DevExpGbb/Secure-Baseline", "devexpgbb/**"),
        ("devexpgbb/secure-baseline", "DevExpGbb/**"),
    ],
)
def test_github_allow_matches_exact_and_owner_glob_in_both_case_directions(
    dependency: str,
    pattern: str,
) -> None:
    result = _check_dependency_allowlist(
        [DependencyReference.parse(dependency)],
        DependencyPolicy(allow=(pattern,)),
    )

    assert result.passed


@pytest.mark.parametrize(
    ("dependency", "pattern"),
    [
        ("DevExpGbb/Secure-Baseline", "devexpgbb/secure-baseline"),
        ("devexpgbb/secure-baseline", "DevExpGbb/Secure-Baseline"),
        ("DevExpGbb/Secure-Baseline", "devexpgbb/**"),
        ("devexpgbb/secure-baseline", "DevExpGbb/**"),
    ],
)
def test_github_deny_blocks_exact_and_owner_glob_in_both_case_directions(
    dependency: str,
    pattern: str,
) -> None:
    result = _check_dependency_denylist(
        [DependencyReference.parse(dependency)],
        DependencyPolicy(deny=(pattern,)),
    )

    assert not result.passed
    assert pattern in result.details[0]


@pytest.mark.parametrize(
    ("dependency", "requirement"),
    [
        ("DevExpGbb/Secure-Baseline", "devexpgbb/secure-baseline"),
        ("devexpgbb/secure-baseline", "DevExpGbb/Secure-Baseline"),
    ],
)
def test_github_require_matches_in_both_case_directions(
    dependency: str,
    requirement: str,
) -> None:
    result = _check_required_packages(
        [DependencyReference.parse(dependency)],
        DependencyPolicy(require=(requirement,)),
    )

    assert result.passed


def test_lowercase_org_policy_workaround_remains_compatible() -> None:
    dependency = DependencyReference.parse("DevExpGbb/zava-agent-config/plugins/secure-baseline")
    allowed_required = DependencyPolicy(
        allow=("devexpgbb/**",),
        require=("devexpgbb/zava-agent-config/plugins/secure-baseline",),
    )
    denied = DependencyPolicy(deny=("devexpgbb/**",))

    assert _check_dependency_allowlist([dependency], allowed_required).passed
    assert _check_required_packages([dependency], allowed_required).passed
    assert not _check_dependency_denylist([dependency], denied).passed


def test_github_exact_repo_does_not_match_different_identity() -> None:
    dependency = DependencyReference.parse("DevExpGbb/secure-baseline-evil")
    policy = DependencyPolicy(allow=("devexpgbb/secure-baseline",))

    assert not _check_dependency_allowlist([dependency], policy).passed


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "DevExpGbb/",
        "DevExpGbb//secure-baseline",
        "https://github.com/DevExpGbb",
        "github.com/DevExpGbb/",
    ],
)
def test_malformed_dependency_identities_remain_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        DependencyReference.parse(raw)


def test_host_qualified_pattern_does_not_match_host_blind_policy_identity() -> None:
    dependency = DependencyReference.parse("DevExpGbb/Secure-Baseline")
    policy = DependencyPolicy(allow=("github.com/DevExpGbb/**",))

    assert not _check_dependency_allowlist([dependency], policy).passed


@pytest.mark.parametrize(
    "dependency",
    [
        "gitlab.com/DevExpGbb/Secure-Baseline",
        "dev.azure.com/DevExpGbb/Factory/_git/Secure-Baseline",
    ],
)
def test_non_github_dependency_matching_remains_case_sensitive(dependency: str) -> None:
    ref = DependencyReference.parse(dependency)
    allow = DependencyPolicy(allow=("devexpgbb/**",))
    deny = DependencyPolicy(deny=("devexpgbb/**",))

    assert not _check_dependency_allowlist([ref], allow).passed
    assert _check_dependency_denylist([ref], deny).passed


def test_github_virtual_path_matching_preserves_repository_path_case() -> None:
    dependency = DependencyReference.parse("DevExpGbb/Secure-Baseline/Packages/My-Skill")

    matching = DependencyPolicy(allow=("devexpgbb/secure-baseline/Packages/**",))
    wrong_path_case = DependencyPolicy(allow=("DevExpGbb/Secure-Baseline/packages/**",))

    assert _check_dependency_allowlist([dependency], matching).passed
    assert not _check_dependency_allowlist([dependency], wrong_path_case).passed


def test_fused_recursive_glob_does_not_change_virtual_path_case() -> None:
    upper_path = DependencyReference.parse("Contoso/Repo/Docs/Secret")
    lower_path = DependencyReference.parse("Contoso/Repo/docs/secret")
    policy = DependencyPolicy(
        allow=("contoso**/Docs/**",),
        deny=("contoso**/Docs/**",),
    )

    assert not _check_dependency_denylist([upper_path], policy).passed
    assert not _check_dependency_allowlist([lower_path], policy).passed


def test_leading_recursive_glob_keeps_ambiguous_literal_case_sensitive() -> None:
    dependency = DependencyReference.parse("DevExpGbb/Secure-Baseline")

    mixed_case = DependencyPolicy(deny=("**/Secure-Baseline",))
    canonical_case = DependencyPolicy(deny=("**/secure-baseline",))

    assert _check_dependency_denylist([dependency], mixed_case).passed
    assert not _check_dependency_denylist([dependency], canonical_case).passed


def test_policy_identity_normalizer_preserves_ref_case() -> None:
    normalized = normalize_package_policy_identity(
        "DevExpGbb/Secure-Baseline#Feature/My-Branch",
        case_insensitive_prefix_segments=2,
    )

    assert normalized == "devexpgbb/secure-baseline#Feature/My-Branch"


def test_generic_glob_and_mcp_matching_remain_case_sensitive() -> None:
    assert not matches_pattern("devexpgbb/secure-baseline", "DevExpGbb/**")

    allowed, _reason = check_mcp_allowed(
        "github-mcp",
        McpPolicy(deny=("GitHub-MCP",)),
    )
    assert allowed


def test_registry_name_constraint_remains_case_sensitive() -> None:
    dependency = DependencyReference(
        repo_url="DevExpGbb/Secure-Baseline",
        source="registry",
        registry_name="Corp",
    )
    policy = RegistrySourcePolicy(
        require=("corp",),
        allow_non_registry=False,
    )

    result = _check_registry_source(
        [dependency],
        policy,
        {"corp": "https://registry.example.test"},
    )

    assert not result.passed


def test_registry_package_reuses_existing_case_insensitive_identity_decision() -> None:
    dependency = DependencyReference(
        repo_url="devexpgbb/team/secure-baseline",
        source="registry",
        registry_name="corp",
    )
    policy = DependencyPolicy(allow=("DevExpGbb/Team/Secure-Baseline",))

    assert dependency.has_case_insensitive_repo_identity
    assert _check_dependency_allowlist([dependency], policy).passed


def test_registry_virtual_path_case_remains_a_package_boundary() -> None:
    dependency = DependencyReference(
        repo_url="acme/widget",
        source="registry",
        registry_name="corp",
        is_virtual=True,
        virtual_path="docs/ok.md",
    )
    allow = DependencyPolicy(allow=("Acme/Widget/Docs/**",))
    require = DependencyPolicy(require=("Acme/Widget/Docs/ok.md",))

    assert not _check_dependency_allowlist([dependency], allow).passed
    assert not _check_required_packages([dependency], require).passed


def test_local_and_marketplace_package_identities_remain_case_sensitive() -> None:
    local = DependencyReference.parse("./Packages/My-Pkg")
    marketplace = DependencyReference(
        repo_url="DevExpGbb/Secure-Baseline",
        is_marketplace=True,
    )
    policy = DependencyPolicy(allow=("devexpgbb/secure-baseline",))

    assert not local.has_case_insensitive_repo_identity
    assert not marketplace.has_case_insensitive_repo_identity
    assert not _check_dependency_allowlist([marketplace], policy).passed


def test_required_package_mixed_case_finds_canonical_lock_entry() -> None:
    dependency = DependencyReference.parse("devexpgbb/secure-baseline")
    lock = _lock_dependency("devexpgbb/secure-baseline")
    policy = DependencyPolicy(require=("DevExpGbb/Secure-Baseline",))

    result = _check_required_packages_deployed([dependency], lock, policy)

    assert result.passed


def test_required_package_mixed_case_cannot_skip_missing_lock_entry() -> None:
    dependency = DependencyReference.parse("devexpgbb/secure-baseline")
    policy = DependencyPolicy(require=("DevExpGbb/Secure-Baseline",))

    result = _check_required_packages_deployed(
        [dependency],
        LockFile(),
        policy,
    )

    assert not result.passed
    assert result.details == ["DevExpGbb/Secure-Baseline"]


def test_required_executable_mixed_case_cannot_skip_untrusted_lock_entry() -> None:
    dependency = DependencyReference.parse("devexpgbb/secure-baseline")
    lock = _lock_dependency(
        "devexpgbb/secure-baseline",
        exec_status="gated_pending_approval",
    )
    policy = ExecutablesPolicy(require=("DevExpGbb/Secure-Baseline",))

    result = _check_required_executable_untrusted([dependency], lock, policy)

    assert not result.passed
    assert result.details == ["DevExpGbb/Secure-Baseline"]


def test_required_version_folds_package_name_but_not_ref() -> None:
    dependency = DependencyReference.parse("devexpgbb/secure-baseline#V1.0.0")
    lock = _lock_dependency(
        "devexpgbb/secure-baseline",
        resolved_ref="v1.0.0",
    )
    policy = DependencyPolicy(
        require=("DevExpGbb/Secure-Baseline#V1.0.0",),
        require_resolution="block",
    )

    result = _check_required_package_version([dependency], lock, policy)

    assert not result.passed
    assert result.details == ["DevExpGbb/Secure-Baseline: expected ref 'V1.0.0', got 'v1.0.0'"]


def test_required_version_matching_ref_still_passes_after_name_normalization() -> None:
    dependency = DependencyReference.parse("devexpgbb/secure-baseline#v1.0.0")
    lock = _lock_dependency(
        "devexpgbb/secure-baseline",
        resolved_ref="v1.0.0",
    )
    policy = DependencyPolicy(
        require=("DevExpGbb/Secure-Baseline#v1.0.0",),
        require_resolution="block",
    )

    assert _check_required_package_version([dependency], lock, policy).passed


def test_required_version_retains_locked_only_mismatch_detail() -> None:
    lock = _lock_dependency(
        "devexpgbb/secure-baseline",
        resolved_ref="v2.0.0",
    )
    policy = DependencyPolicy(
        require=("devexpgbb/secure-baseline#v1.0.0",),
        require_resolution="block",
    )

    result = _check_required_package_version([], lock, policy)

    assert not result.passed
    assert result.details == ["devexpgbb/secure-baseline: expected ref 'v1.0.0', got 'v2.0.0'"]
