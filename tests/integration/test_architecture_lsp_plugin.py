"""Architecture guardrails for the Claude LSP plugin owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]


def test_lsp_owner_rules_pass() -> None:
    """The live source must satisfy both registered LSP ownership rules."""
    report = run_selected_rules(
        ROOT,
        (
            "install-deployment-executable-trust-context",
            "install-deployment-lsp-target-contract",
            "install-deployment-lsp-lifecycle",
        ),
    )

    assert report.exit_code == 2
    assert report.violations == ()
    assert report.failures == ()


def test_claude_lsp_plugin_path_bypass_is_rejected() -> None:
    """The LSP rule must reject a direct project-path write."""
    path = "src/apm_cli/integration/lsp_integrator.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "return BaseIntegrator.resolve_deploy_path(relative_path, project_root)"
    assert old in source
    mutated = source.replace(old, "return spec.path(project_root, user_scope=False)", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-target-contract",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-lsp-target-contract"
    }


def test_user_lsp_config_path_bypass_is_rejected() -> None:
    """The LSP rule must reject a direct user-config path write."""
    path = "src/apm_cli/integration/lsp_integrator.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "allowed_prefixes=(relative_path,)"
    assert old in source
    mutated = source.replace(old, "allowed_prefixes=()", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-target-contract",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-lsp-target-contract"
    }


def test_lsp_target_ownership_bypass_is_rejected() -> None:
    """The LSP rule must require target-scoped state at the lifecycle owner."""
    path = "src/apm_cli/install/lsp/integration.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "lsp_target_servers=new_targets"
    assert old in source
    mutated = source.replace(old, "lsp_target_servers={}")

    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-lifecycle",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {"install-deployment-lsp-lifecycle"}


def test_lsp_lifecycle_direct_call_bypass_is_rejected() -> None:
    path = "src/apm_cli/install/services.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source + "\nLSPIntegrator.install([])\n"

    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-lifecycle",),
        source_overrides={path: mutated},
    )

    assert {item.rule_id for item in report.violations} == {"install-deployment-lsp-lifecycle"}


def test_claude_lsp_approval_alias_bypass_is_rejected() -> None:
    """The LSP rule must reject local approval-key derivation."""
    path = "src/apm_cli/integration/lsp_integrator.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "locked_dependency_approval_keys(locked_dependency)"
    assert old in source
    mutated = source.replace(old, "(locked_dependency.name,)", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-target-contract",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-lsp-target-contract"
    }


def test_local_bundle_content_approval_bypass_is_rejected() -> None:
    """The executable-trust rule must require content-bound bundle consent."""
    path = "src/apm_cli/install/local_bundle_handler.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "bundle_approval_key = local_bundle_approval_key("
    assert old in source
    mutated = source.replace(old, "bundle_approval_key = build_approval_key(", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-executable-trust-context",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-executable-trust-context"
    }


def test_local_bundle_canvas_approval_bypass_is_rejected() -> None:
    path = "src/apm_cli/install/services.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "is_package_approved(allow_executables, approval_key, EXEC_TYPE_CANVAS)"
    assert old in source
    mutated = source.replace(
        old,
        "is_package_approved(allow_executables, slug, EXEC_TYPE_CANVAS)",
        1,
    )

    report = run_selected_rules(
        ROOT,
        ("install-deployment-executable-trust-context",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-executable-trust-context"
    }


def test_multitarget_lsp_preflight_bypass_is_rejected() -> None:
    path = "src/apm_cli/integration/lsp_integrator.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "prepared_targets.append((runtime, spec, prepared))"
    assert old in source
    mutated = source.replace(old, "pass  # removed multi-target preflight", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-target-contract",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-lsp-target-contract"
    }
