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


# -- Dependency allow/deny -----------------------------------------


# -- Required packages ---------------------------------------------


# -- Required version + project-wins semantics ---------------------


# -- MCP checks in resolved set ------------------------------------


# -- Target / compilation checks -----------------------------------


# -- Fail-fast vs run-all ------------------------------------------


# -- Disk-level checks NOT included --------------------------------


# -- Mixed scenario: multiple checks with project-wins combo -------


# -- Registry source policy (gap #5 verification) -----------------


class TestRegistrySourcePolicyWiring:
    """Verify gap #5: does ``registries=`` flow correctly into the check?

    Each test calls ``run_dependency_policy_checks`` with the same args
    used by the install pipeline (``policy_gate``) and varies only the
    ``registries=`` kwarg. The behavior difference IS the bug.
    """

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
