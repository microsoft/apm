"""Tests for ``run_dependency_policy_checks`` -- the resolved-dep policy seam (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import pytest

from apm_cli.policy.models import CheckResult, CIAuditResult  # noqa: F401
from apm_cli.policy.policy_checks import (
    run_dependency_policy_checks,
)
from apm_cli.policy.schema import (
    ApmPolicy,
    RegistrySourcePolicy,
)

pytestmark = pytest.mark.component

# -- Helpers --------------------------------------------------------


def _make_dep_refs(dep_strings: list[str]):
    """Parse a list of dep strings into DependencyReference objects."""
    from apm_cli.models.apm_package import DependencyReference

    return [DependencyReference.parse(s) for s in dep_strings]


# -- Dependency allow/deny -----------------------------------------


# -- Required packages ---------------------------------------------


# -- Required version + project-wins semantics ---------------------


# -- MCP checks in resolved set ------------------------------------


# -- Target / compilation checks -----------------------------------


# -- Fail-fast vs run-all ------------------------------------------


# -- Disk-level checks NOT included --------------------------------


# -- Mixed scenario: multiple checks with project-wins combo -------


# -- Registry source policy (gap #5 verification) -----------------


def _make_registry_dep(repo_url: str, registry_name: str, reference: str = "^1.0.0"):
    """Build a DependencyReference for a registry-sourced dep."""
    from apm_cli.models.apm_package import DependencyReference

    return DependencyReference(
        repo_url=repo_url,
        reference=reference,
        source="registry",
        registry_name=registry_name,
    )


class TestRegistrySourcePolicyWiring:
    """Verify gap #5: does ``registries=`` flow correctly into the check?

    Each test calls ``run_dependency_policy_checks`` with the same args
    used by the install pipeline (``policy_gate``) and varies only the
    ``registries=`` kwarg. The behavior difference IS the bug.
    """

    def test_no_registries_kwarg_still_fails_closed(self):
        """When the caller does NOT pass ``registries=`` (legacy callers
        or callers without manifest access), the check must fail-closed:
        any ``policy.require`` name is treated as unconfigured.

        This is correct fail-closed behavior. Callers with manifest
        access (install pipeline, audit --ci) should always pass
        ``registries=`` so users with correctly-wired registries are
        not falsely blocked -- see the next test.
        """
        deps = [_make_registry_dep("acme/foo", "corp-main")]
        policy = ApmPolicy(
            registry_source=RegistrySourcePolicy(require=("corp-main",)),
        )
        result = run_dependency_policy_checks(deps, policy=policy)
        reg_checks = [c for c in result.checks if c.name == "registry-source"]
        assert reg_checks, "registry-source check should have run"
        assert not reg_checks[0].passed
        assert any("corp-main" in d for d in (reg_checks[0].details or []))

    def test_correctly_configured_registry_passes_when_registries_passed(self):
        """Fix path: passing ``registries={'corp-main': '<url>'}`` lets
        the check distinguish 'configured' from 'unreachable' and the
        install proceeds.
        """
        deps = [_make_registry_dep("acme/foo", "corp-main")]
        policy = ApmPolicy(
            registry_source=RegistrySourcePolicy(require=("corp-main",)),
        )
        result = run_dependency_policy_checks(
            deps,
            policy=policy,
            registries={"corp-main": "https://registry.corp.example.com"},
        )
        reg_checks = [c for c in result.checks if c.name == "registry-source"]
        assert reg_checks, "registry-source check should have run"
        assert reg_checks[0].passed, (
            "with registries= passed, a correctly-routed dep should pass; "
            "got: " + str(reg_checks[0].details)
        )

    def test_config_json_only_registry_counts_as_configured(self, tmp_path, monkeypatch):
        """Merged config.json registries on APMPackage satisfy policy.require."""
        import apm_cli.config as _conf
        from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {
                "experimental": {"registries": True},
                "registries": {
                    "corp-main": {
                        "url": "https://registry.corp.example.com",
                        "default": True,
                    }
                },
            },
        )
        clear_apm_yml_cache()
        p = tmp_path / "apm.yml"
        p.write_text("name: x\nversion: 1.0.0\ndependencies:\n  apm:\n    - acme/foo#^1.0.0\n")
        pkg = APMPackage.from_apm_yml(p)
        policy = ApmPolicy(
            registry_source=RegistrySourcePolicy(require=("corp-main",)),
        )
        result = run_dependency_policy_checks(
            pkg.get_apm_dependencies(),
            policy=policy,
            registries=pkg.registries,
        )
        reg_checks = [c for c in result.checks if c.name == "registry-source"]
        assert reg_checks and reg_checks[0].passed

    def test_unconfigured_required_registry_blocks_with_clear_message(self):
        """Spec behavior: forgot to configure ``corp-main`` in apm.yml.
        Caller passes an empty registries map. The check must fail-closed
        with a message naming the missing registry.
        """
        deps = [_make_registry_dep("acme/foo", "corp-main")]
        policy = ApmPolicy(
            registry_source=RegistrySourcePolicy(require=("corp-main",)),
        )
        result = run_dependency_policy_checks(
            deps,
            policy=policy,
            registries={},  # caller plumbed the map; it's just empty
        )
        reg_checks = [c for c in result.checks if c.name == "registry-source"]
        assert reg_checks and not reg_checks[0].passed
        assert any("corp-main" in d for d in (reg_checks[0].details or [])), (
            "violation message should name the missing registry"
        )

    def test_no_op_when_policy_empty(self):
        """Sanity: empty policy.require + allow_non_registry=True is no-op
        regardless of registries= argument.
        """
        deps = _make_dep_refs(["owner/git-pkg"])
        policy = ApmPolicy()  # default RegistrySourcePolicy()
        result = run_dependency_policy_checks(deps, policy=policy)
        reg_checks = [c for c in result.checks if c.name == "registry-source"]
        assert reg_checks and reg_checks[0].passed
