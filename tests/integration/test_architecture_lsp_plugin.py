"""Architecture guardrails for the Claude LSP plugin owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]


def test_claude_lsp_plugin_owner_rules_pass() -> None:
    """The live source must satisfy both registered LSP ownership rules."""
    report = run_selected_rules(
        ROOT,
        (
            "install-deployment-executable-trust-context",
            "install-deployment-claude-lsp-plugin",
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
        ("install-deployment-claude-lsp-plugin",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {"install-deployment-claude-lsp-plugin"}


def test_claude_lsp_approval_alias_bypass_is_rejected() -> None:
    """The LSP rule must reject local approval-key derivation."""
    path = "src/apm_cli/integration/lsp_integrator.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "locked_dependency_approval_keys(locked_dependency)"
    assert old in source
    mutated = source.replace(old, "(locked_dependency.name,)", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-claude-lsp-plugin",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {"install-deployment-claude-lsp-plugin"}
