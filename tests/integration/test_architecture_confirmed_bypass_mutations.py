"""Regression mutations for architecture-linter bypasses found in review."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.xdist_group(name="architecture_confirmed_bypass_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]
EXEMPT = "architecture-authority-exempt: review regression"


@dataclass(frozen=True)
class BypassMutation:
    name: str
    rule_id: str
    path: str
    old: str | None = None
    new: str | None = None
    append: str = ""
    replace_all: bool = False


BYPASS_MUTATIONS: tuple[BypassMutation, ...] = (
    BypassMutation(
        name="target-context-exemption",
        rule_id="registry_delegation.install_target_selection",
        path="src/apm_cli/commands/install.py",
        append=(
            "\n\ndef _review_rogue_target_context(ctx):\n"
            f"    return dict(target_context=(ctx.target,))  # {EXEMPT}\n"
        ),
    ),
    BypassMutation(
        name="symlink-owner-exemption",
        rule_id="transport-platform-url-path-security",
        path="src/apm_cli/utils/net.py",
        append=(f"\n\ndef has_symlink_component(path):  # {EXEMPT}\n    return False\n"),
    ),
    BypassMutation(
        name="transport-selection-exemption",
        rule_id="transport-platform-git-semver-preflight",
        path="src/apm_cli/marketplace/ref_resolver.py",
        append=f"\n\nTransportSelector()  # {EXEMPT}\n",
    ),
    BypassMutation(
        name="semver-guard-exemption",
        rule_id="transport-platform-git-semver-preflight",
        path="src/apm_cli/commands/install.py",
        append=f'\n\n_REVIEW_SEMVER = dep_ref.ref_kind == "semver"  # {EXEMPT}\n',
    ),
    BypassMutation(
        name="credential-branch-exemption",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/install/pipeline.py",
        append=(
            "\n\ndef _review_rogue_credential_branch(host, is_generic):\n"
            "    if is_generic or is_azure_devops_hostname(host):  "
            f"# {EXEMPT}\n        return True\n    return False\n"
        ),
    ),
    BypassMutation(
        name="duplicate-runtime-resolver",
        rule_id="mutation_writes.mcp_target_selection",
        path="src/apm_cli/integration/mcp_integrator_install.py",
        append=("\n\ndef _resolve_target_runtimes(*args, **kwargs):\n    return ()\n"),
    ),
    BypassMutation(
        name="duplicate-locked-collector",
        rule_id="mutation_writes.mcp_declaration_scope",
        path="src/apm_cli/integration/mcp_config_view.py",
        append=("\n\ndef _collect_locked_dependencies(*args, **kwargs):\n    return ()\n"),
    ),
    BypassMutation(
        name="doctor-status-dead-name-only",
        rule_id="registry_delegation.output_diagnostics",
        path="src/apm_cli/commands/marketplace/__init__.py",
        old=(
            "    if not check.passed:\n"
            '        return STATUS_SYMBOLS["warning"] if check.informational '
            'else STATUS_SYMBOLS["error"]\n'
            '    return STATUS_SYMBOLS["info"] if check.informational '
            'else STATUS_SYMBOLS["check"]'
        ),
        new=("    if False:\n        STATUS_SYMBOLS\n    return check.symbol"),
    ),
    BypassMutation(
        name="doctor-status-wrong-import",
        rule_id="registry_delegation.output_diagnostics",
        path="src/apm_cli/commands/marketplace/__init__.py",
        old="from ...utils.console import STATUS_SYMBOLS",
        new="from ...marketplace.models import STATUS_SYMBOLS",
    ),
    BypassMutation(
        name="doctor-status-wrong-return",
        rule_id="registry_delegation.output_diagnostics",
        path="src/apm_cli/commands/marketplace/__init__.py",
        old=(
            '        return STATUS_SYMBOLS["warning"] if check.informational '
            'else STATUS_SYMBOLS["error"]'
        ),
        new="        return check.symbol",
    ),
    BypassMutation(
        name="agent-plugin-duplicate-exemption",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/marketplace/resolver.py",
        append=(f"\n\ndef detect_agent_plugin(path):  # {EXEMPT}\n    return None\n"),
    ),
    BypassMutation(
        name="asset-inventory-digest-md5",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/agent_plugins/assets.py",
        old="            digest = hashlib.sha256()\n            bytes_read = 0",
        new="            digest = hashlib.md5()\n            bytes_read = 0",
    ),
    BypassMutation(
        name="asset-verification-digest-md5",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/agent_plugins/assets.py",
        old="        digest = hashlib.sha256()\n        while chunk :=",
        new="        digest = hashlib.md5()\n        while chunk :=",
    ),
    BypassMutation(
        name="asset-hashlib-wrong-import",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/agent_plugins/assets.py",
        old="import hashlib",
        new="import rogue_hashlib as hashlib",
    ),
    BypassMutation(
        name="asset-hashlib-rebinding",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/agent_plugins/assets.py",
        append="\n\nhashlib = object()\n",
    ),
    BypassMutation(
        name="asset-hashlib-local-shadow",
        rule_id="marketplace-integrations-agent-plugin-contract",
        path="src/apm_cli/agent_plugins/assets.py",
        old="            digest = hashlib.sha256()\n            bytes_read = 0",
        new=(
            "            hashlib = rogue_hashlib\n"
            "            digest = hashlib.sha256()\n"
            "            bytes_read = 0"
        ),
    ),
    BypassMutation(
        name="transport-checkout-duplicate-effective-class",
        rule_id="transport-platform-host-credential-resolution",
        path="src/apm_cli/deps/github_downloader.py",
        append=(
            "\n\nclass GitHubPackageDownloader:\n"
            "    def _persistent_cache_checkout(self, *args, **kwargs):\n"
            "        return None\n"
        ),
    ),
    BypassMutation(
        name="marketplace-output-path-duplicate-effective-definition",
        rule_id="marketplace-integrations-output-path",
        path="src/apm_cli/marketplace/output_profiles.py",
        append=(
            "\n\ndef resolve_effective_output_path(*args, **kwargs):\n"
            "    return Path('/tmp/rogue')\n"
        ),
    ),
    BypassMutation(
        name="marketplace-local-path-duplicate-effective-definition",
        rule_id="marketplace-integrations-local-audit-resolution",
        path="src/apm_cli/marketplace/resolver.py",
        append=(
            "\n\ndef resolve_local_plugin_path(*args, **kwargs):\n    return Path('/tmp/rogue')\n"
        ),
    ),
    BypassMutation(
        name="wrapped-bootstrap-resolver-result",
        rule_id="registry_delegation.bootstrap_project_name",
        path="src/apm_cli/commands/init.py",
        old=("final_project_name = _resolve_bootstrap_project_name(derived_project_name)"),
        new=(
            'final_project_name = _resolve_bootstrap_project_name(derived_project_name) or "rogue"'
        ),
    ),
)


def _mutated_source(case: BypassMutation) -> str:
    source = (ROOT / case.path).read_text(encoding="utf-8")
    if case.old is not None:
        assert case.new is not None
        expected = source.count(case.old)
        if case.replace_all:
            assert expected >= 1
            source = source.replace(case.old, case.new)
        else:
            assert expected == 1
            source = source.replace(case.old, case.new, 1)
    source += case.append
    ast.parse(source, filename=case.path)
    return source


@pytest.mark.parametrize(
    "case",
    BYPASS_MUTATIONS,
    ids=[case.name for case in BYPASS_MUTATIONS],
)
def test_confirmed_bypass_is_rejected(case: BypassMutation) -> None:
    """Each formerly clean bypass now produces its owning rule's violation."""
    report = run_selected_rules(
        ROOT,
        (case.rule_id,),
        source_overrides={case.path: _mutated_source(case)},
    )

    assert report.failures == ()
    assert report.exit_code == 2
    assert any(violation.rule_id == case.rule_id for violation in report.violations)
