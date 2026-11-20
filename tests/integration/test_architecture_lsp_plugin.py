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
            "install-deployment-lsp-capability-gate",
        ),
    )

    assert report.exit_code == 2
    assert report.violations == ()
    assert report.failures == ()


def test_claude_lsp_plugin_path_bypass_is_rejected() -> None:
    """The LSP rule must reject a direct project-path write."""
    path = "src/apm_cli/integration/lsp_integrator.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = (
        "        return BaseIntegrator.resolve_deploy_path(\n"
        "            relative_path,\n"
        "            root,\n"
        "            allowed_prefixes=(relative_path,),\n"
        "        )\n"
    )
    assert old in source
    mutated = source.replace(
        old, "        return spec.path(project_root, user_scope=user_scope)\n", 1
    )

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


def test_approve_original_identity_grant_is_rejected() -> None:
    """``apm approve`` must grant the key the gate checks, not the original (#3710)."""
    path = "src/apm_cli/commands/approve.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "approval_identity=approval.identity"
    assert old in source
    mutated = source.replace(old, "approval_identity=locked.get_unique_key()", 1)

    report = run_selected_rules(
        ROOT,
        ("install-deployment-executable-trust-context",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert {item.rule_id for item in report.violations} == {
        "install-deployment-executable-trust-context"
    }


def test_approve_parallel_replacement_derivation_is_rejected() -> None:
    """A second "is this row replaced" decision beside the owner is refused."""
    path = "src/apm_cli/commands/approve.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source + "\n\ndef _is_replaced(locked):\n    return locked.replacement is not None\n"

    report = run_selected_rules(
        ROOT,
        ("install-deployment-executable-trust-context",),
        source_overrides={path: mutated},
    )

    assert report.exit_code == 2
    assert [item.rule_id for item in report.violations] == [
        "install-deployment-executable-trust-context"
    ]


def test_local_bundle_canvas_approval_bypass_is_rejected() -> None:
    path = "src/apm_cli/install/services.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    old = "allow_executables, _bundle_keys, EXEC_TYPE_CANVAS"
    assert old in source
    mutated = source.replace(old, "allow_executables, (slug,), EXEC_TYPE_CANVAS", 1)

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


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/service.py",
            '"""Application Service',
            '_GAP = "no effective target supports LSP"\n"""Application Service',
        ),
        (
            "src/apm_cli/install/service_integration.py",
            "    prepared_lsp.require_capable_target()\n",
            "",
        ),
        (
            "src/apm_cli/install/lsp/integration.py",
            "        _resolve_lsp_target_runtimes(\n            project_root=ctx.project_root,",
            "        LSPIntegrator.resolve_target_runtimes(\n            project_root=ctx.project_root,",
        ),
    ],
    ids=["second-refusal-site", "mcp-before-gate", "second-target-resolution"],
)
def test_lsp_capability_gate_split_is_rejected(path: str, old: str, new: str) -> None:
    """A second capability-gap refusal, or MCP writes before the gate, are rejected."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert source.count(old) == 1
    report = run_selected_rules(
        ROOT,
        ("install-deployment-lsp-capability-gate",),
        source_overrides={path: source.replace(old, new, 1)},
    )

    assert {item.rule_id for item in report.violations} == {
        "install-deployment-lsp-capability-gate"
    }
