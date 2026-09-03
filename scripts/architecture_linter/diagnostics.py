"""Collect-then-render diagnostics: push during the run, render once at the end.

Mirrors the ``DiagnosticCollector`` pattern already used elsewhere in APM
(push diagnostics during an operation, render a summary at the end) so the
architecture linter's output shape is familiar rather than inventing a new
one. :class:`DiagnosticCollector` is the push side, used by the runner while
it works; :func:`render_violations_and_failures` (and the ``format_*``
functions it is built from) is the render side, used once the run is over.

Output is plain ASCII, one finding per line, Ruff-diagnostic shaped:
``path:line:column: semantic-rule-id message``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from scripts.architecture_linter.models import Failure, Violation

_FAILURE_RULE_ID = "architecture-linter-failure"

# Temporary display-only bridge from the semantic IDs to headings in the
# retired ``scripts/lint-architecture-boundaries.sh`` artifact. Several old AC
# numbers were reused; the semantic rule ID remains the primary, unambiguous
# identifier. Entries are intentionally explicit and conservative: rules with
# no clean historical heading are left unmapped rather than assigned a guess.
LEGACY_AC_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "contracts-tests-executable-contract-authorities": ("AC9",),
        "contracts-tests-lifecycle-smoke-partition": ("AC25",),
        "contracts-tests-taxonomy-classification": ("AC22",),
        "contracts-tooling-ado-lock-coordinates": ("AC14",),
        "contracts-tooling-apply-to-placement": ("AC31",),
        "contracts-tooling-cached-policy-shape": ("AC3",),
        "contracts-tooling-dependency-identity": ("AC23", "AC25", "AC29"),
        "contracts-tooling-frontmatter-yaml": ("AC36",),
        "contracts-tooling-lockfile-timestamp": ("AC2",),
        "contracts-tooling-lockfile-timestamp-constructor": ("AC2",),
        "contracts-tooling-lockfile-timestamp-fallback": ("AC2",),
        "install-deployment-approval-outcome-routing": ("AC3",),
        "install-deployment-audit-policy-discovery": ("AC3",),
        "install-deployment-audit-replay": ("AC4",),
        "install-deployment-cached-claude-skill-metadata": ("AC4",),
        "install-deployment-dependency-winner-selection": ("AC4",),
        "install-deployment-deployment-frame-projection": ("AC4",),
        "install-deployment-frozen-mutation-eligibility": ("AC27",),
        "install-deployment-git-object-field-authority": ("AC4",),
        "install-deployment-gitlab-facade-orchestration": ("AC3",),
        "install-deployment-gitlab-policy-adapter": ("AC3",),
        "install-deployment-incomplete-chain-routing": ("AC3",),
        "install-deployment-local-bundle-policy-preflight": ("AC3",),
        "install-deployment-local-identity-anchor": ("AC4",),
        "install-deployment-locked-skill-subset-reconstruction": ("AC4",),
        "install-deployment-manifest-inheritance-includes": ("AC3",),
        "install-deployment-marketplace-mutation-lock": ("AC7",),
        "install-deployment-mcp-ownership-migration": ("AC21",),
        "install-deployment-mcp-registry-resolution": ("AC4",),
        "install-deployment-package-target-authorization": ("AC15b",),
        "install-deployment-plugin-bin-eligibility": ("AC3",),
        "install-deployment-provenance-state": ("AC4", "AC18"),
        "install-deployment-ref-recheck-ownership": ("AC4",),
        "install-deployment-registry-dependency-intent": ("AC4",),
        "install-deployment-request-defaults": ("AC1",),
        "install-deployment-require-hashes-enforcement": ("AC3",),
        "install-deployment-resolution-replacement": ("AC36",),
        "install-deployment-resolver-queue-dedup": ("AC4",),
        "install-deployment-skill-subset-tokens": ("AC4",),
        "install-deployment-source-plan": ("AC3",),
        "install-deployment-target-file-contraction": ("AC4", "AC15a"),
        "install-deployment-uninstall-reachability": ("AC16",),
        "install-deployment-uninstall-selection": ("AC4",),
        "install-deployment-update-plan-ref-annotation": ("AC4",),
        "marketplace-integrations-agent-plugin-contract": ("AC1",),
        "marketplace-integrations-bundle-format-authority": ("AC1",),
        "marketplace-integrations-catalog-manifest": ("AC35",),
        "marketplace-integrations-copilot-ownership": ("AC35",),
        "marketplace-integrations-generated-bundle-lf-writers": ("AC2",),
        "marketplace-integrations-hash-visible-lf-writers": ("AC34",),
        "marketplace-integrations-legacy-skill-membership": ("AC1",),
        "marketplace-integrations-local-audit-resolution": ("AC10b",),
        "marketplace-integrations-native-registration": ("AC35",),
        "marketplace-integrations-output-path": ("AC31",),
        "marketplace-integrations-package-construction": ("AC1",),
        "marketplace-integrations-package-projection": ("AC1",),
        "marketplace-integrations-producer-admission": ("AC1",),
        "marketplace-integrations-projection-boundary": ("AC1", "AC2"),
        "marketplace-integrations-raw-diagnostics": ("AC33",),
        "marketplace-integrations-removed-plugin-lifecycle": ("AC1",),
        "marketplace-integrations-source-parsing": ("AC10",),
        "marketplace-integrations-tag-pattern": ("AC27",),
        "marketplace-integrations-version-precedence": ("AC30",),
        "mutation_writes.copilot_cli_mcp_paths": ("AC1",),
        "mutation_writes.drift_hook_membership": ("AC4",),
        "mutation_writes.hook_cleanup_scope": ("AC15",),
        "mutation_writes.hook_command_vocabulary": ("AC15d",),
        "mutation_writes.jetbrains_mcp_path": ("AC28",),
        "mutation_writes.mcp_declaration_scope": ("AC25",),
        "mutation_writes.mcp_package_launcher": ("AC26", "AC30", "AC32"),
        "mutation_writes.mcp_target_selection": ("AC21",),
        "mutation_writes.neutral_hook_contract": ("AC2", "AC4", "AC6", "AC15", "AC15c"),
        "mutation_writes.user_root_scope": ("AC1",),
        "registry_delegation.agents_source_attribution": ("AC2",),
        "registry_delegation.bootstrap_project_name": ("AC18",),
        "registry_delegation.command_machine_output": ("AC5",),
        "registry_delegation.compile_inventory_authority": ("AC2",),
        "registry_delegation.compiled_output_writes": ("AC2",),
        "registry_delegation.diagnostic_ascii_owner": ("AC12",),
        "registry_delegation.experimental_target_hints": ("AC1",),
        "registry_delegation.host_backend_dispatch": ("AC1",),
        "registry_delegation.install_target_selection": ("AC1",),
        "registry_delegation.lifecycle_docs_aggregate": ("AC6",),
        "registry_delegation.lockfile_version_authority": ("AC2",),
        "registry_delegation.logger_redaction_attachment": ("AC5",),
        "registry_delegation.manifest_schema_negotiation": ("AC2",),
        "registry_delegation.native_locator_target_names": ("AC1",),
        "registry_delegation.output_diagnostics": ("AC12",),
        "registry_delegation.policy_ref_redaction": ("AC5",),
        "registry_delegation.root_cli_output_mode": ("AC5",),
        "registry_delegation.runtime_descriptors": ("AC1",),
        "registry_delegation.target_vocabulary": ("AC1",),
        "transport-platform-git-cache-identity": ("AC11",),
        "transport-platform-git-semver-preflight": ("AC13",),
        "transport-platform-github-throttle": ("AC17",),
        "transport-platform-host-credential-resolution": (
            "AC5",
            "AC19",
            "AC20",
            "AC24",
            "AC26",
        ),
        "transport-platform-network-host-parsing": ("AC1",),
        "transport-platform-ref-freshness": ("AC4",),
        "transport-platform-runtime-deadline-safety": ("AC7",),
        "transport-platform-self-update-resolution": ("AC26",),
        "transport-platform-sparse-symlink-validation": ("AC11a",),
        "transport-platform-tls-trust-injection": ("AC5",),
        "transport-platform-url-path-security": ("AC3", "AC10a"),
        "transport-platform-windows-stable-path": ("AC8",),
    }
)


def _ascii_text(value: str) -> str:
    """Escape non-printable or non-ASCII text without dropping information."""
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 32 <= codepoint <= 126:
            escaped.append(character)
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)


def format_violation(violation: Violation) -> str:
    """Render one violation as ``path:line:column: rule-id message``."""
    aliases = LEGACY_AC_ALIASES.get(violation.rule_id, ())
    alias_metadata = f" [legacy {','.join(aliases)}]" if aliases else ""
    return (
        f"{_ascii_text(violation.path)}:{violation.line}:{violation.column}: "
        f"{_ascii_text(violation.rule_id)}{alias_metadata} {_ascii_text(violation.message)}"
    )


def format_failure(failure: Failure) -> str:
    """Render one aggregated startup/read/parse/registry/rule failure.

    Failures have no single source line, so they are rendered against a
    synthetic ``1:1`` location tagged with their stage, keeping the same
    grep-able ``path:line:column: rule-id message`` shape as violations.
    """
    return f"{_ascii_text(failure.stage)}:1:1: {_FAILURE_RULE_ID} {_ascii_text(failure.message)}"


def _violation_sort_key(violation: Violation) -> tuple[str, int, int, str]:
    return (violation.path, violation.line, violation.column, violation.rule_id)


def _failure_sort_key(failure: Failure) -> tuple[str, str]:
    return (failure.stage, failure.message)


class DiagnosticCollector:
    """Accumulates violations and failures while the run is in progress."""

    def __init__(self) -> None:
        self._violations: list[Violation] = []
        self._failures: list[Failure] = []

    def add_violation(self, violation: Violation) -> None:
        self._violations.append(violation)

    def add_failure(self, failure: Failure) -> None:
        self._failures.append(failure)

    @property
    def violations(self) -> tuple[Violation, ...]:
        """Every collected violation, sorted deterministically."""
        return tuple(sorted(self._violations, key=_violation_sort_key))

    @property
    def failures(self) -> tuple[Failure, ...]:
        """Every collected failure, sorted deterministically."""
        return tuple(sorted(self._failures, key=_failure_sort_key))

    @property
    def has_findings(self) -> bool:
        return bool(self._violations or self._failures)

    def render(self) -> str:
        """Render every collected finding as sorted, ASCII, Ruff-style lines."""
        lines = [format_violation(v) for v in self.violations]
        lines.extend(format_failure(f) for f in self.failures)
        return "\n".join(lines)


def render_violations_and_failures(
    violations: tuple[Violation, ...], failures: tuple[Failure, ...]
) -> str:
    """Render an already-sorted violations/failures pair as final CLI text."""
    lines = [format_violation(v) for v in sorted(violations, key=_violation_sort_key)]
    lines.extend(format_failure(f) for f in sorted(failures, key=_failure_sort_key))
    return "\n".join(lines)
