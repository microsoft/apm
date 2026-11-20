"""MCP target-selection, declaration-scope, and launcher mutation analyzers.

Ports three of the owner guards recorded in
``.apm/architecture/owners/hooks-integrations.json``:

* ``hooks-integrations-mcp-target-selection`` -- AC21 MCP manifest target
  precedence.
* ``hooks-integrations-mcp-declaration-scope`` -- AC25 root vs dependency MCP
  scope.
* ``hooks-integrations-mcp-package-launcher`` -- AC26/AC30/AC32
  MCPClientAdapter launcher selection and argv shape.
* ``hooks-integrations-mcp-passthrough-denylist`` -- MCP passthrough modeled
  fields and harness aliases.
* ``hooks-integrations-jetbrains-mcp-path`` -- AC28 JetBrains Copilot MCP
  path.
* ``hooks-integrations-mcp-json-config-write`` -- MCP JSON config
  parse-for-update and atomic write owned by MCPClientAdapter.
* ``hooks-integrations-plugin-root-render`` -- portable MCP/LSP
  ``${CLAUDE_PLUGIN_ROOT}`` binding, per-target render and legacy equivalence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from scripts.architecture_linter.checks.mutation_write_shared import (
    _MCP_OWNERSHIP,
    _SRC,
    GROUP,
    _count_regex_lines,
    _duplicate_scan,
    _first_span_line,
    _function_span,
    _has_fixed,
    _has_regex,
    _python_paths,
    _read_required,
    _require,
    _span_has_fixed,
    _span_has_regex,
    _span_lines,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_ADAPTERS_CLIENT = "src/apm_cli/adapters/client/"


_MCP_INSTALL = "src/apm_cli/integration/mcp_integrator_install.py"


_MCP_INTEGRATION = "src/apm_cli/install/mcp/integration.py"


_LSP_INTEGRATOR = "src/apm_cli/integration/lsp_integrator.py"


_APM_PACKAGE = "src/apm_cli/models/apm_package.py"


_MCP_SCOPE_OWNER = "src/apm_cli/integration/mcp_config_view.py"
# LSP owns its own ``bundle:`` provenance namespace.
_LSP_BUNDLE_OWNER = "src/apm_cli/install/lsp/integration.py"
_EXECUTABLES_OWNER = "src/apm_cli/security/executables.py"
# Uninstall only intersects with managed servers and never admits one.
_MCP_RAW_VIEW_CONSUMER = "src/apm_cli/commands/uninstall/engine.py"
_MCP_AUDIT_CONSUMER = "src/apm_cli/policy/ci_checks.py"
# Audit's org-policy wrapper judges the MCP servers the audit view keeps.
_MCP_POLICY_AUDIT = "src/apm_cli/policy/policy_checks.py"


_MCP_ADAPTER_BASE = "src/apm_cli/adapters/client/base.py"


_MCP_MODEL = "src/apm_cli/models/dependency/mcp.py"


_MCP_OPENCODE = "src/apm_cli/adapters/client/opencode.py"


_MCP_CONTAINER_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/adapters/client/copilot.py",
    "src/apm_cli/adapters/client/codex.py",
    "src/apm_cli/adapters/client/gemini.py",
    "src/apm_cli/adapters/client/vscode.py",
)


_MCP_NONCONTAINER_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/adapters/client/copilot.py",
    "src/apm_cli/adapters/client/vscode.py",
)


_MCP_VSCODE = "src/apm_cli/adapters/client/vscode.py"


_INTELLIJ_OWNER = "src/apm_cli/adapters/client/intellij.py"


def _function_spans(
    facts: FileFacts, name: str, *, top_level: bool = True
) -> tuple[tuple[int, int], ...]:
    """Return every matching function span so duplicate owners stay visible."""
    return tuple(
        (definition.line, definition.end_line)
        for definition in facts.definitions
        if definition.name == name
        and definition.kind == "function"
        and (definition.scope == "<module>") is top_level
    )


def _sum_regex_lines(
    provider: FactsProvider, rule_id: str, paths: Sequence[str], pattern: str
) -> int:
    """Return the total ``grep -Ec`` line count of `pattern` across `paths`."""
    compiled = re.compile(pattern)
    total = 0
    for path in paths:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            continue
        total += sum(1 for line in facts.lines if compiled.search(line) is not None)
    return total


def _missing_consumers(
    provider: FactsProvider, rule_id: str, consumers: Sequence[str], needle: str, message: str
) -> tuple[Violation, ...]:
    """Report each consumer missing `needle` (mirrors ``grep -L``)."""
    findings: list[Violation] = []
    for path in consumers:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        if not _has_fixed(facts, needle):
            findings.append(violation(rule_id, path, message))
    return tuple(findings)


def _check_mcp_target_selection(provider: FactsProvider) -> Iterable[Violation]:
    """MCP target precedence must route through the manifest adapter first.

    Ports the AC21 guard: the manifest adapter parses targets and carries no
    target vocabulary, the resolver consults the manifest adapter before
    runtime discovery and never parses targets itself, ``run_mcp_integration``
    validates before install, and the canonical projection returns the
    ``{"target": ..., "targets": ...}`` shape.
    """
    rule_id = "mutation_writes.mcp_target_selection"
    facts_by_path, failures = _read_required(
        provider, rule_id, (_MCP_INSTALL, _MCP_INTEGRATION, _APM_PACKAGE, _LSP_INTEGRATOR)
    )
    if failures:
        return failures

    install_facts = facts_by_path[_MCP_INSTALL]
    integration_facts = facts_by_path[_MCP_INTEGRATION]
    package_facts = facts_by_path[_APM_PACKAGE]

    adapter_span = _function_span(install_facts, "_declared_manifest_target_runtimes")
    resolver_spans = _function_spans(install_facts, "_resolve_target_runtimes")
    resolver_span = resolver_spans[0] if len(resolver_spans) == 1 else None
    integration_span = _function_span(integration_facts, "run_mcp_integration")
    projection_span = _function_span(package_facts, "canonical_package_target_config")

    missing = [
        (span_name, owner_path)
        for span_name, span, owner_path in (
            ("_declared_manifest_target_runtimes", adapter_span, _MCP_INSTALL),
            ("_resolve_target_runtimes", resolver_span, _MCP_INSTALL),
            ("run_mcp_integration", integration_span, _MCP_INTEGRATION),
            ("canonical_package_target_config", projection_span, _APM_PACKAGE),
        )
        if span is None
    ]
    if len(resolver_spans) != 1:
        return (
            violation(
                rule_id,
                _MCP_INSTALL,
                "_resolve_target_runtimes must be defined exactly once "
                f"(found {len(resolver_spans)})",
            ),
        )
    if missing:
        return tuple(
            violation(rule_id, owner_path, f"MCP target owner function {name} is missing")
            for name, owner_path in missing
        )
    if (
        adapter_span is None
        or resolver_span is None
        or integration_span is None
        or projection_span is None
    ):  # unreachable past the missing guard; narrows the optional spans.
        return ()

    findings: list[Violation] = []
    if not _span_has_fixed(install_facts, adapter_span, "manifest_target_restriction(apm_config)"):
        findings.append(
            violation(
                rule_id, _MCP_INSTALL, "manifest adapter must parse targets from the manifest"
            )
        )
    if _span_has_regex(
        install_facts,
        adapter_span,
        r"TARGET_CAPABILITIES|CANONICAL_TARGETS|KNOWN_TARGETS|"
        r"\[[^\]]*(copilot|claude|cursor|codex|gemini|opencode|windsurf|kiro)",
    ):
        findings.append(
            violation(rule_id, _MCP_INSTALL, "manifest adapter must not embed target vocabulary")
        )
    manifest_line = _first_span_line(
        install_facts, resolver_span, "_declared_manifest_target_runtimes(apm_config)"
    )
    discovery_line = _first_span_line(install_facts, resolver_span, "_discover_installed_runtimes(")
    if manifest_line is None or discovery_line is None or manifest_line >= discovery_line:
        findings.append(
            violation(
                rule_id,
                _MCP_INSTALL,
                "resolver must consult the manifest adapter before runtime discovery",
            )
        )
    if _span_has_fixed(install_facts, resolver_span, "parse_targets_field(") or _span_has_fixed(
        install_facts, resolver_span, "manifest_target_restriction("
    ):
        findings.append(
            violation(rule_id, _MCP_INSTALL, "resolver must not parse manifest targets itself")
        )
    # C4 (#3967): MCP and LSP, project and user scope, read --exclude through one owner.
    lsp_facts = facts_by_path[_LSP_INTEGRATOR]
    for facts, span, path, call in (
        (
            install_facts,
            resolver_span,
            _MCP_INSTALL,
            "filter_excluded_mcp_runtimes(target_runtimes, exclude)",
        ),
        (
            lsp_facts,
            _function_span(lsp_facts, "resolve_target_runtimes", top_level=False),
            _LSP_INTEGRATOR,
            "filter_excluded_mcp_runtimes(candidates, exclude)",
        ),
    ):
        if (
            span is None
            or not _span_has_fixed(facts, span, call)
            or _span_has_regex(facts, span, r"= \{exclude\b|\(exclude, \"--exclude\"\)")
        ):
            findings.append(
                violation(
                    rule_id,
                    path,
                    "resolver must apply --exclude through filter_excluded_mcp_runtimes",
                )
            )
    validation_line = _first_span_line(
        integration_facts,
        integration_span,
        "package_target_declaration(apm_package).validate_for_install()",
    )
    install_line = _first_span_line(integration_facts, integration_span, "MCPIntegrator.install(")
    if validation_line is None or install_line is None or validation_line >= install_line:
        findings.append(
            violation(
                rule_id,
                _MCP_INTEGRATION,
                "run_mcp_integration must validate targets before MCPIntegrator.install",
            )
        )
    if not _span_has_fixed(
        package_facts, projection_span, 'return {"target": singular, "targets": list(plural)}'
    ):
        findings.append(
            violation(
                rule_id,
                _APM_PACKAGE,
                "canonical target projection must return the target/targets shape",
            )
        )
    for fragment in (
        ").validate_config_for_install()",
        "_raise_strict_config_failures(unsafe_runtimes, console=console, logger=logger)",
        "_validate_target_configs(\n        target_runtimes,",
    ):
        findings.extend(
            _require(
                _has_fixed(install_facts, fragment),
                rule_id,
                _MCP_INSTALL,
                "Selected MCP targets must delegate safe-config preflight before no-op detection",
            )
        )
    findings.extend(_dependency_target_selection_findings(provider, rule_id, install_facts))
    return findings


# Per-dependency ``targets:`` narrowing (#2606): one owner decides which runtimes
# receive a server; install writes, claim pruning and --frozen claims route
# through it instead of re-reading ``dep.targets``.
_MCP_DEPENDENCY_TARGET_CONSUMERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        _MCP_INSTALL,
        "run_mcp_install",
        (
            "mcp_dependency_runtimes(dep, target_runtimes)",
            "selected = {target: mcp_servers_for_runtime(mcp_deps, target) for target in",
            "managed_target_servers[target] &= selected[target]",
            "transfer_deselected_shared_claims(",
        ),
    ),
    (
        _MCP_INTEGRATION,
        "run_mcp_integration",
        (
            "admitted = mcp_servers_for_runtime(mcp_deps, owned_target)",
            "mcp_deps, owned_target, named_only=True",
            "deselected_mcp_claims(",
            "target_decision.from_manifest",
        ),
    ),
    (
        _MCP_INTEGRATION,
        "run_owned_mcp_integration",
        ("mcp_servers_for_runtime(dependencies, target)", "deselected_mcp_claims("),
    ),
    (
        "src/apm_cli/install/service_integration.py",
        "run_service_integrations",
        ("mcp_servers_for_runtime(prepared_mcp.dependencies, target)",),
    ),
)


def _dependency_target_selection_findings(
    provider: FactsProvider, rule_id: str, install_facts: FileFacts
) -> list[Violation]:
    findings: list[Violation] = []
    owner_spans = _function_spans(install_facts, "mcp_dependency_runtimes")
    if len(owner_spans) != 1 or not _span_has_fixed(
        install_facts, owner_spans[0], "declared = _declared_mcp_targets(dep)"
    ):
        findings.append(
            violation(
                rule_id,
                _MCP_INSTALL,
                "mcp_dependency_runtimes must be the one per-dependency MCP target owner",
            )
        )
    for path, function, fragments in _MCP_DEPENDENCY_TARGET_CONSUMERS:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        span = _function_span(facts, function)
        for fragment in fragments:
            if span is None or not _span_has_fixed(facts, span, fragment):
                findings.append(
                    violation(
                        rule_id,
                        path,
                        f"{function} must route MCP dependency targets through "
                        f"mcp_dependency_runtimes (missing {fragment!r})",
                    )
                )
    return findings


def _check_mcp_declaration_scope(provider: FactsProvider) -> Iterable[Violation]:
    """Transitive MCP dependency scope must use production-only collection.

    Ports the AC25 guard: ``CurrentMcpConfigView.derive`` reads
    ``root.get_all_mcp_dependencies()`` exactly once, the locked and unlocked
    collectors never call it, and each resolves ``package.get_mcp_dependencies()``
    exactly once.
    """
    rule_id = "mutation_writes.mcp_declaration_scope"
    facts, failures = checked_facts(provider, _MCP_SCOPE_OWNER, rule_id, require_python=True)
    if failures:
        return failures

    derive_span = _function_span(facts, "derive", top_level=False)
    locked_spans = _function_spans(facts, "_collect_locked_dependencies")
    locked_span = locked_spans[0] if len(locked_spans) == 1 else None
    unlocked_span = _function_span(facts, "_collect_unlocked_compat")
    missing = [
        (name, span)
        for name, span in (
            ("derive", derive_span),
            ("_collect_locked_dependencies", locked_span),
            ("_collect_unlocked_compat", unlocked_span),
        )
        if span is None
    ]
    if len(locked_spans) != 1:
        return (
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "_collect_locked_dependencies must be defined exactly once "
                f"(found {len(locked_spans)})",
            ),
        )
    if missing:
        return tuple(
            violation(rule_id, _MCP_SCOPE_OWNER, f"MCP declaration-scope owner {name} is missing")
            for name, _ in missing
        )
    if derive_span is None or locked_span is None or unlocked_span is None:
        return ()  # unreachable past the missing guard; narrows the optional spans.

    findings: list[Violation] = []
    if _count_span_fixed(facts, derive_span, "root.get_all_mcp_dependencies()") != 1:
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "derive must read root.get_all_mcp_dependencies exactly once",
            )
        )
    if _span_has_fixed(facts, locked_span, "get_all_mcp_dependencies()") or _span_has_fixed(
        facts, unlocked_span, "get_all_mcp_dependencies()"
    ):
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "dependency collectors must not read the aggregate MCP dependency view",
            )
        )
    if _count_span_fixed(facts, locked_span, "package.get_mcp_dependencies()") != 1:
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "locked collector must read package.get_mcp_dependencies exactly once",
            )
        )
    if _count_span_fixed(facts, unlocked_span, "package.get_mcp_dependencies()") != 1:
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "unlocked collector must read package.get_mcp_dependencies exactly once",
            )
        )
    if not _span_has_fixed(
        facts, locked_span, "mcp_dependency.requires_transitive_trust"
    ) or not _span_has_fixed(facts, unlocked_span, "dependency.requires_transitive_trust"):
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "dependency collectors must gate transitive declarations through "
                "MCPDependency.requires_transitive_trust",
            )
        )
    if not _span_has_fixed(
        facts, derive_span, "active_package_keys=active_package_keys,"
    ) or not _span_has_fixed(
        facts,
        locked_span,
        "active_package_keys is not None and package_key not in active_package_keys",
    ):
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "MCP source scope must preserve supplied complete active graph membership",
            )
        )
    if not _span_has_fixed(facts, locked_span, "build_download_ref(") or not _span_has_fixed(
        facts, locked_span, "require_locked=True,"
    ):
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "Pending MCP sources must use the locked acquisition owner",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_MCP_SCOPE_OWNER,)),
            pattern=r"^\s*def pending_servers\(|_absent_apm_package_mcp_configs",
            message="Deferred MCP origin evidence belongs to CurrentMcpConfigView",
            exempt=False,
        )
    )
    if not _span_has_fixed(facts, derive_span, "_bundle_owned_baseline(lockfile"):
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "derive must keep recorded local-bundle MCP servers as baseline",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(
                provider, under=_SRC, exclude=(_MCP_SCOPE_OWNER, _LSP_BUNDLE_OWNER)
            ),
            pattern=r"""f?["']bundle:(?:["']|\{)""",
            message="Local-bundle MCP owner tokens belong to CurrentMcpConfigView",
            exempt=False,
        )
    )
    findings.extend(_check_bundle_baseline_admission(provider, rule_id, facts))
    return findings


def _check_bundle_baseline_admission(
    provider: FactsProvider, rule_id: str, facts: FileFacts
) -> list[Violation]:
    """A ``bundle:`` claim is baseline only with its receipt, and audit policy judges it.

    The baseline owner requires the lock's MCP deployment receipt, and the
    audit policy wrapper judges exactly the view's kept servers: apm.yml
    servers alone only when there is no lock to keep anything.
    """
    findings: list[Violation] = []
    baseline_span = _function_span(facts, "_bundle_owned_baseline")
    if (
        baseline_span is None
        or not _span_has_fixed(
            facts, baseline_span, "DeploymentLedgerCodec.mcp_native_hashes(lockfile)"
        )
        or not _span_has_fixed(facts, baseline_span, "and name in deployed")
    ):
        findings.append(
            violation(
                rule_id,
                _MCP_SCOPE_OWNER,
                "a bundle: provenance is baseline only with the lock's MCP deployment receipt",
            )
        )
    policy, failures = checked_facts(provider, _MCP_POLICY_AUDIT, rule_id, require_python=True)
    if failures:
        return [*findings, *failures]
    span = _function_span(policy, "run_policy_checks")
    lines = [text.strip() for _, text in _span_lines(policy, span)] if span else []
    reads = [index for index, text in enumerate(lines) if "get_all_mcp_dependencies()" in text]
    if (
        not span
        or "view, _admission = audit_mcp_view(" not in lines
        or "mcp_deps = list(view.kept_dependencies())" not in lines
        or "mcp_brought_in_by=mcp_brought_in_by," not in lines
        or len(reads) != 1
        or lines[reads[0] - 1] != "if lock is None:"
    ):
        findings.append(
            violation(
                rule_id,
                _MCP_POLICY_AUDIT,
                "audit MCP policy must judge every server CurrentMcpConfigView keeps",
            )
        )
    return findings


def _count_span_fixed(facts: FileFacts, span: tuple[int, int], needle: str) -> int:
    """Count lines in `span` containing fixed `needle`."""
    return sum(1 for _, text in _span_lines(facts, span) if needle in text)


def _check_mcp_omission_evidence(provider: FactsProvider) -> Iterable[Violation]:
    """Persistent MCP omission decisions must remain in the current-view owner."""
    rule_id = "mutation_writes.mcp_omission_evidence"
    required = {
        _MCP_SCOPE_OWNER: (
            "def omission_evidence(",
            "def with_recorded_omissions(",
            "def _declaration_fingerprint(",
            '"dependency": dependency.get_unique_key()',
            '"declaration": declaration.to_dict()',
            "if valid and name not in admitted:",
            "def consent_problems(",
            "verdict = admission.admit(subject)",
            '"reason": str(verdict.reason)',
            "REASON_UNAPPROVED: self.gated_names,",
            "def select(",
            "admission: McpAdmission,",
            "recorded: LockFile | None,",
            "view = view.with_recorded_omissions(recorded)",
            "view = replace(view, selection_replayed=True)",
            "evidence == self.omissions.get(name, False)",
        ),
        "src/apm_cli/policy/ci_checks.py": (
            "CurrentMcpConfigView.select(",
            "recorded=lock,",
            "exact=False,",
        ),
        "src/apm_cli/install/mcp/integration.py": (
            "include_dependency_mcp=should_install",
            "return self.omission_view.omission_evidence(lockfile)",
            "omissions = prepared.omissions(lockfile)",
            "admission = McpAdmission.for_project(",
            "admission=admission,",
            "current_view = CurrentMcpConfigView.select(",
            "recorded=None,",
            "current_view if should_install else None",
        ),
        # #3925, #3902: both --frozen MCP checks admit through the trust owner
        # and verify recorded evidence.
        "src/apm_cli/install/service.py": (
            "current = CurrentMcpConfigView.select(",
            "admission=admission,",
            "recorded=lockfile,",
        ),
        "src/apm_cli/install/phases/integrate.py": (
            "admission=McpAdmission.for_project(",
            'trust_context=getattr(effective_allow_map(ctx), "trust_context", None)',
        ),
    }
    facts_by_path, failures = _read_required(provider, rule_id, tuple(required))
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)
    for path, fragments in required.items():
        for fragment in fragments:
            findings.extend(
                _require(
                    _has_fixed(facts_by_path[path], fragment),
                    rule_id,
                    path,
                    "MCP omission evidence must retain dependency/declaration binding and owner routing",
                )
            )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_MCP_SCOPE_OWNER,)),
            pattern=(
                r"^\s*def (?:omission_evidence|with_recorded_omissions"
                r"|consent_problems|_declaration_fingerprint)\("
            ),
            message="MCP omission selection and comparison belong to CurrentMcpConfigView",
            exempt=False,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_MCP_SCOPE_OWNER,)),
            pattern=r"\.with_(?:recorded_omissions|unapproved_executables)\(",
            message="Lockfile comparisons derive the MCP view through CurrentMcpConfigView.select",
            exempt=False,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(
                provider, under=_SRC, exclude=(_MCP_SCOPE_OWNER, _MCP_RAW_VIEW_CONSUMER)
            ),
            pattern=r"CurrentMcpConfigView\.derive\(",
            message="Lockfile comparisons must not skip the executables gate or evidence",
            exempt=False,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(
                provider, under=_SRC, exclude=(_MCP_SCOPE_OWNER, _MCP_AUDIT_CONSUMER)
            ),
            pattern=r"\bexact=False\b",
            message="Only audit accepts structural omission evidence (it re-checks consent)",
            exempt=False,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_EXECUTABLES_OWNER,)),
            pattern=r"\bfilter_mcp_by_allow_executables\(",
            message="MCP executables approval routes through security/trust.py McpAdmission",
            exempt=False,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_EXECUTABLES_OWNER,)),
            pattern=r"resolved_by\b.*\bis not None\b.*\bis_self_defined\b",
            message="MCP admission belongs to security/trust.py McpAdmission",
            exempt=False,
        )
    )
    return tuple(findings)


_PLUGIN_ROOT_OWNER = "src/apm_cli/deps/plugin_root_render.py"
_RAW_STDIO_READ_RE = r"""(?:\.get\(\s*|\[\s*)["']_raw_stdio["']"""


def _check_plugin_root_render(provider: FactsProvider) -> Iterable[Violation]:
    """Recorded MCP/LSP state keeps the authored plugin root; only renders expand it."""
    rule_id = "mutation_writes.plugin_root_render"
    required = {
        _PLUGIN_ROOT_OWNER: (
            "def bind_plugin_roots(",
            "def render_plugin_root(",
            "def finalize_project_root(",
            "def legacy_absolute_equivalent(",
        ),
        _APM_PACKAGE: ('if kind in ("mcp", "lsp")', "bind_plugin_roots("),
        _MCP_ADAPTER_BASE: (
            "def _self_defined_stdio(",
            "finalize_project_root(",
            "def __init_subclass__(",
        ),
        _MCP_SCOPE_OWNER: ("legacy_absolute_equivalent(", "equivalent=self._legacy_equivalent"),
        _MCP_INTEGRATION: ("current_view.replay_recorded_roots(mcp_deps, old_mcp_configs)",),
        "src/apm_cli/integration/lsp_integrator.py": ("LSPIntegrator._render_plugin_roots(",),
    }
    facts_by_path, failures = _read_required(provider, rule_id, tuple(required))
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)
    for path, fragments in required.items():
        for fragment in fragments:
            findings.extend(
                _require(
                    _has_fixed(facts_by_path[path], fragment),
                    rule_id,
                    path,
                    "MCP/LSP plugin roots must stay authored in recorded state and expand "
                    "only through deps/plugin_root_render.py at the target render",
                )
            )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_ADAPTERS_CLIENT, exclude=(_MCP_ADAPTER_BASE,)),
            pattern=_RAW_STDIO_READ_RE,
            message="Adapters read self-defined stdio through MCPClientAdapter._self_defined_stdio",
            exempt=False,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_PLUGIN_ROOT_OWNER,)),
            pattern=r"^\s*def (?:render_plugin_root|legacy_absolute_equivalent|_substitute_plugin_root)\(",
            message="Plugin-root rendering and legacy equivalence belong to deps/plugin_root_render.py",
            exempt=False,
        )
    )
    return tuple(findings)


def _check_mcp_package_launcher(provider: FactsProvider) -> Iterable[Violation]:
    """MCP launcher selection and argv shape must route through the adapter.

    Ports AC26 (container image argument), AC30 (non-container launcher argv
    and retired ``_extract_package_args``), and AC32 (runtime variable
    substitution) -- every one owned by ``adapters/client/base.py``
    (``MCPClientAdapter``).
    """
    rule_id = "mutation_writes.mcp_package_launcher"
    base_facts, failures = checked_facts(provider, _MCP_ADAPTER_BASE, rule_id, require_python=True)
    if failures:
        return failures

    adapter_paths = _python_paths(provider, under=_ADAPTERS_CLIENT)
    findings: list[Violation] = []
    findings.extend(_mpl_container(provider, rule_id, base_facts, adapter_paths))
    findings.extend(_mpl_non_container(provider, rule_id, base_facts, adapter_paths))
    findings.extend(_mpl_runtime_variables(provider, rule_id, base_facts, adapter_paths))
    return findings


def _mpl_container(
    provider: FactsProvider, rule_id: str, base_facts: FileFacts, adapter_paths: Sequence[str]
) -> tuple[Violation, ...]:
    """AC26: container launcher decisions route through MCPClientAdapter."""
    findings: list[Violation] = []
    findings.extend(
        _require(
            _has_fixed(base_facts, '_REGISTRY_TYPE_ALIASES = {"oci": "docker"}'),
            rule_id,
            _MCP_ADAPTER_BASE,
            "MCPClientAdapter must own the OCI-to-docker registry alias",
        )
    )
    findings.extend(
        _require(
            _sum_regex_lines(
                provider, rule_id, adapter_paths, r"^\s*def _ensure_docker_image_arg\("
            )
            == 1,
            rule_id,
            _MCP_ADAPTER_BASE,
            "_ensure_docker_image_arg must be defined exactly once across client adapters",
        )
    )
    findings.extend(
        _missing_consumers(
            provider,
            rule_id,
            _MCP_CONTAINER_CONSUMERS,
            "_ensure_docker_image_arg(",
            "container consumer must route image arguments through MCPClientAdapter",
        )
    )
    # #3404: the `docker run -i --rm` default and options-only normalization
    # have one owner; consumers must not re-grow an inline default.
    findings.extend(
        _require(
            _sum_regex_lines(provider, rule_id, adapter_paths, r"^\s*def _ensure_docker_run_args\(")
            == 1,
            rule_id,
            _MCP_ADAPTER_BASE,
            "_ensure_docker_run_args must be defined exactly once across client adapters",
        )
    )
    findings.extend(
        _missing_consumers(
            provider,
            rule_id,
            _MCP_CONTAINER_CONSUMERS,
            "_ensure_docker_run_args(",
            "container consumer must route docker run arguments through MCPClientAdapter",
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_MCP_CONTAINER_CONSUMERS,
            pattern=r"\bor\s+\[\s*[\"']run[\"']",
            message="inline docker run default bypasses MCPClientAdapter._ensure_docker_run_args",
            exempt=False,
        )
    )
    return tuple(findings)


def _mpl_non_container(
    provider: FactsProvider, rule_id: str, base_facts: FileFacts, adapter_paths: Sequence[str]
) -> tuple[Violation, ...]:
    """AC30: non-container launcher argv routes through MCPClientAdapter."""
    findings: list[Violation] = []
    findings.extend(
        _require(
            _sum_regex_lines(
                provider, rule_id, adapter_paths, r"^\s*def _build_non_container_launcher_argv\("
            )
            == 1,
            rule_id,
            _MCP_ADAPTER_BASE,
            "_build_non_container_launcher_argv must be defined exactly once",
        )
    )
    findings.extend(
        _require(
            _has_fixed(base_facts, "cls._build_non_container_launcher_argv("),
            rule_id,
            _MCP_ADAPTER_BASE,
            "MCPClientAdapter must build the non-container launcher argv",
        )
    )
    findings.extend(
        _missing_consumers(
            provider,
            rule_id,
            _MCP_NONCONTAINER_CONSUMERS,
            "self._build_non_container_launcher_argv(",
            "non-container consumer must route argv through MCPClientAdapter",
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=adapter_paths,
            pattern=r"_extract_package_args\(",
            message="retired _extract_package_args must not be called",
            exempt=False,
            exclude_line_pattern=r"^\s*def _extract_package_args\(",
        )
    )
    return tuple(findings)


def _mpl_runtime_variables(
    provider: FactsProvider, rule_id: str, base_facts: FileFacts, adapter_paths: Sequence[str]
) -> tuple[Violation, ...]:
    """AC32: runtime argument variables route through MCPClientAdapter."""
    findings: list[Violation] = []
    findings.extend(
        _require(
            _sum_regex_lines(
                provider, rule_id, adapter_paths, r"^\s*def _substitute_runtime_variables\("
            )
            == 1,
            rule_id,
            _MCP_ADAPTER_BASE,
            "_substitute_runtime_variables must be defined exactly once",
        )
    )
    findings.extend(
        _require(
            _has_regex(base_facts, r"^    def _substitute_runtime_variables\("),
            rule_id,
            _MCP_ADAPTER_BASE,
            "MCPClientAdapter must own _substitute_runtime_variables",
        )
    )
    vscode_facts, vscode_failures = checked_facts(
        provider, _MCP_VSCODE, rule_id, require_python=True
    )
    if vscode_failures:
        findings.extend(vscode_failures)
    else:
        findings.extend(
            _require(
                _has_fixed(vscode_facts, "cls._substitute_runtime_variables("),
                rule_id,
                _MCP_VSCODE,
                "VS Code adapter must route runtime variables through MCPClientAdapter",
            )
        )
    return tuple(findings)


def _check_mcp_passthrough_denylist(provider: FactsProvider) -> Iterable[Violation]:
    """MCP passthrough filtering must route through the base adapter denylist."""
    rule_id = "mutation_writes.mcp_passthrough_denylist"
    facts_by_path, failures = _read_required(
        provider,
        rule_id,
        (_MCP_MODEL, _MCP_ADAPTER_BASE, _MCP_OPENCODE),
    )
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)

    findings.extend(
        _require(
            _has_fixed(
                facts_by_path[_MCP_ADAPTER_BASE],
                "from ...models.dependency.mcp import _EXTRA_DENYLIST, TrustedEnvLiteral",
            ),
            rule_id,
            _MCP_ADAPTER_BASE,
            "base adapter must import the canonical passthrough denylist",
        )
    )
    findings.extend(
        _require(
            _has_fixed(
                facts_by_path[_MCP_MODEL],
                "_HARNESS_EXTRA_ALIASES = frozenset(\n"
                "    {\n"
                '        "bearer_token_env_var",\n'
                '        "enabled",\n'
                '        "env_http_headers",\n'
                '        "environment",\n'
                '        "httpUrl",\n'
                '        "http_headers",\n'
                '        "id",\n'
                '        "serverUrl",\n'
                "    }\n"
                ")",
            ),
            rule_id,
            _MCP_MODEL,
            "MCP model must own the harness-alias denylist",
        )
    )
    findings.extend(
        _require(
            _has_fixed(
                facts_by_path[_MCP_MODEL],
                "_EXTRA_DENYLIST = _RESERVED_EXTRA_KEYS | _HARNESS_EXTRA_ALIASES",
            ),
            rule_id,
            _MCP_MODEL,
            "MCP model must combine modeled fields and harness aliases",
        )
    )
    findings.extend(
        _require(
            _has_fixed(
                facts_by_path[_MCP_OPENCODE],
                "from ...models.dependency.mcp import _EXTRA_DENYLIST",
            ),
            rule_id,
            _MCP_OPENCODE,
            "OpenCode adapter must import the canonical passthrough denylist",
        )
    )
    findings.extend(
        _require(
            _has_fixed(
                facts_by_path[_MCP_OPENCODE],
                "translated_keys = _EXTRA_DENYLIST",
            ),
            rule_id,
            _MCP_OPENCODE,
            "OpenCode translation must derive exclusions from the canonical denylist",
        )
    )
    return tuple(findings)


def _check_opencode_enabled_intent(provider: FactsProvider) -> Iterable[Violation]:
    """OpenCode consumers must retain the model's presence and trust boundary."""
    rule_id = "mutation_writes.opencode_enabled_intent"
    integrator = "src/apm_cli/integration/mcp_integrator.py"
    required = {
        _MCP_MODEL: (
            'OPENCODE_ENABLED_KEY = "_apm_opencode_enabled"',
            "return intent.value if isinstance(intent, OpenCodeEnabled) else default",
            'if ("enabled" in current) != ("enabled" in stored):',
            'json.dumps(current["enabled"], sort_keys=True)',
            'stored["enabled"], sort_keys=True',
            "return self.enabled is not _ENABLED_UNSET",
            "server_info.pop(OPENCODE_ENABLED_KEY, None)",
            "server_info[OPENCODE_ENABLED_KEY] = OpenCodeEnabled(self.enabled)",
            'enabled=d.get("enabled", _ENABLED_UNSET)',
            'if self.has_enabled:\n            result["enabled"] = self.enabled',
        ),
        integrator: (
            "from apm_cli.models.dependency.mcp import opencode_enabled_matches",
            "not opencode_enabled_matches(current_config, stored)",
        ),
        _MCP_OPENCODE: (
            "from ...models.dependency.mcp import _EXTRA_DENYLIST, opencode_enabled_value",
            "enabled=opencode_enabled_value(server_info, enabled)",
        ),
        _MCP_OWNERSHIP: ("opencode_enabled_matches(existing[name], expected)",),
    }
    facts_by_path, failures = _read_required(provider, rule_id, tuple(required))
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)
    for path, fragments in required.items():
        for fragment in fragments:
            findings.extend(
                _require(
                    _has_fixed(facts_by_path[path], fragment),
                    rule_id,
                    path,
                    "OpenCode enabled intent must retain the canonical model contract",
                )
            )
    findings.extend(
        _require(
            _count_regex_lines(
                facts_by_path[integrator], r"^\s+dep\.apply_opencode_enabled\(info\)$"
            )
            == 2,
            rule_id,
            integrator,
            "Both self-defined and registry overlays must use the enabled owner",
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_MCP_MODEL,)),
            pattern=r"_apm_opencode_enabled|^\s*(?:def opencode_enabled_|class OpenCodeEnabled)",
            message="OpenCode enabled vocabulary and decisions belong to the MCP model",
            exempt=False,
        )
    )
    return tuple(findings)


def _check_jetbrains_mcp_path(provider: FactsProvider) -> Iterable[Violation]:
    """JetBrains Copilot MCP config paths must come from the IntelliJ adapter.

    Ports the AC28 guard: the IntelliJ adapter owns ``_intellij_config_dir``
    and ``_legacy_intellij_config_dir`` (each defined once), and no other
    module derives a ``github-copilot`` IntelliJ path.
    """
    rule_id = "mutation_writes.jetbrains_mcp_path"
    facts, failures = checked_facts(provider, _INTELLIJ_OWNER, rule_id, require_python=True)
    findings: list[Violation] = list(failures)
    if not failures:
        findings.extend(
            _require(
                _count_regex_lines(facts, r"^def _intellij_config_dir\(") == 1,
                rule_id,
                _INTELLIJ_OWNER,
                "_intellij_config_dir must be defined exactly once",
            )
        )
        findings.extend(
            _require(
                _count_regex_lines(facts, r"^def _legacy_intellij_config_dir\(") == 1,
                rule_id,
                _INTELLIJ_OWNER,
                "_legacy_intellij_config_dir must be defined exactly once",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_INTELLIJ_OWNER,)),
            pattern=r"github-copilot.{0,80}intellij|intellij.{0,80}github-copilot",
            message="JetBrains Copilot MCP paths must come from the IntelliJ adapter",
            exempt=True,
        )
    )
    return tuple(findings)


def _check_mcp_json_config_write(provider: FactsProvider) -> Iterable[Violation]:
    """MCP JSON config merge-writes must route through the base adapter owner.

    ``MCPClientAdapter`` owns parse-for-update (refuse to rewrite a file it
    cannot parse, or a project-scope config that is a symbolic link) and the
    atomic writer; no other adapter may serialize JSON straight into a config
    file handle or rewrite an unparsable config from scratch.
    """
    rule_id = "mutation_writes.mcp_json_config_write"
    facts, failures = checked_facts(provider, _MCP_ADAPTER_BASE, rule_id, require_python=True)
    findings: list[Violation] = list(failures)
    if not failures:
        for owner in ("_parse_json_config_for_update", "_write_json_config"):
            findings.extend(
                _require(
                    _count_regex_lines(facts, rf"^\s+def {owner}\(") == 1,
                    rule_id,
                    _MCP_ADAPTER_BASE,
                    f"{owner} must be defined exactly once on MCPClientAdapter",
                )
            )
            span = _function_span(facts, owner, top_level=False)
            findings.extend(
                _require(
                    span is not None
                    and _span_has_fixed(facts, span, "self._is_linked_project_config("),
                    rule_id,
                    _MCP_ADAPTER_BASE,
                    f"{owner} must refuse project-scope MCP configs that are symlinks",
                )
            )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_ADAPTERS_CLIENT, exclude=(_MCP_ADAPTER_BASE,)),
            pattern=r"json\.dump\(|write_text\(\s*json\.dumps|open\([^)]*[\"']w[\"']",
            message="MCP JSON config writes must use MCPClientAdapter._write_json_config",
            exempt=True,
        )
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_ADAPTERS_CLIENT, exclude=(_MCP_ADAPTER_BASE,)),
            pattern=r"rewriting from scratch",
            message="Unparsable MCP configs must fail closed via _parse_json_config_for_update",
            exempt=True,
        )
    )
    return tuple(findings)


def _check_mcp_ownership_migration(provider: FactsProvider) -> Iterable[Violation]:
    """Legacy MCP target ownership migration must stay owned by one module.

    Ports the AC21 companion guard: ``install/mcp/ownership.py`` owns
    ``migrate_legacy_project_target_servers``, the MCP install integrator
    calls it, and no other module redefines it. This carries no owner guard
    because the owner (``install/mcp/ownership.py``) is outside the
    hooks-integrations owner set, but the decision is MCP-integration
    semantics in this group's domain.
    """
    rule_id = "mutation_writes.mcp_ownership_migration"
    facts_by_path, failures = _read_required(provider, rule_id, (_MCP_OWNERSHIP, _MCP_INSTALL))
    findings: list[Violation] = list(failures)
    if not failures:
        findings.extend(
            _require(
                _has_regex(
                    facts_by_path[_MCP_OWNERSHIP], r"^def migrate_legacy_project_target_servers\("
                ),
                rule_id,
                _MCP_OWNERSHIP,
                "install/mcp/ownership.py must own migrate_legacy_project_target_servers",
            )
        )
        findings.extend(
            _require(
                _has_fixed(facts_by_path[_MCP_INSTALL], "migrate_legacy_project_target_servers("),
                rule_id,
                _MCP_INSTALL,
                "MCP install integrator must call migrate_legacy_project_target_servers",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_MCP_OWNERSHIP,)),
            pattern=r"^\s*def migrate_legacy_project_target_servers\(",
            message="legacy MCP target ownership migration must stay owned by install/mcp/ownership.py",
            exempt=False,
        )
    )
    return tuple(findings)


# -- MCP config-key derivation owner guard ----------------------------------

_MCP_CONFIG_KEY_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/integration/mcp_integrator.py",
    "src/apm_cli/integration/mcp_integrator_install.py",
    "src/apm_cli/install/mcp/ownership.py",
)

# Pattern that re-derives a config key from a server name by splitting on "/"
# outside the canonical owner.  The adapter's determine_config_key already
# handles scoped-npm, self-defined, and owner/repo fallback cases; a bare
# split in a consumer would diverge.
_INLINE_KEY_DERIVATION_RE = re.compile(r"""(?:rsplit|split)\(\s*["']/["']\s*[,)].*\[-1\]""")


def _check_mcp_config_key_derivation(provider: FactsProvider) -> Iterable[Violation]:
    """MCP config-key derivation must route through MCPClientAdapter.determine_config_key.

    The adapter owns the scoped-npm, self-defined and owner/repo -> short-name
    fallback logic.  Consumers must call through the adapter, not re-derive a
    config key from a server name by splitting on ``"/"``.
    """
    rule_id = "mutation_writes.mcp_config_key_derivation"
    base_facts, failures = checked_facts(provider, _MCP_ADAPTER_BASE, rule_id, require_python=True)
    if failures:
        return failures

    findings: list[Violation] = []

    # Owner must define the canonical static method.
    if not _has_fixed(base_facts, "def determine_config_key("):
        findings.append(
            violation(
                rule_id,
                _MCP_ADAPTER_BASE,
                "MCPClientAdapter must define determine_config_key as the config-key owner",
            )
        )

    # Consumers must route through the owner, not inline slash-based key derivation.
    findings.extend(
        _missing_consumers(
            provider,
            rule_id,
            _MCP_CONFIG_KEY_CONSUMERS,
            "determine_config_key(",
            "MCP config-key consumer must route through MCPClientAdapter.determine_config_key",
        )
    )

    for path in _MCP_CONFIG_KEY_CONSUMERS:
        facts, f = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(f)
        if f:
            continue
        for lineno, line in enumerate(facts.lines, 1):
            if _INLINE_KEY_DERIVATION_RE.search(line) is not None:
                findings.append(
                    violation(
                        rule_id,
                        path,
                        "MCP config-key must not be re-derived by splitting on '/'; "
                        "route through MCPClientAdapter.determine_config_key",
                        line=lineno,
                    )
                )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id="mutation_writes.mcp_config_key_derivation",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-config-key-derivation",),
        description="MCP config-key derivation must route through MCPClientAdapter.determine_config_key.",
        check=_check_mcp_config_key_derivation,
    ),
    Rule(
        id="mutation_writes.mcp_target_selection",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-target-selection",),
        description="MCP target precedence must route through the manifest adapter first.",
        check=_check_mcp_target_selection,
    ),
    Rule(
        id="mutation_writes.mcp_declaration_scope",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-declaration-scope",),
        description="Root vs dependency MCP declaration scope must use production-only collection.",
        check=_check_mcp_declaration_scope,
    ),
    Rule(
        id="mutation_writes.mcp_omission_evidence",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-omission-evidence",),
        description="Dependency MCP omission evidence and comparison use the current-view owner.",
        check=_check_mcp_omission_evidence,
    ),
    Rule(
        id="mutation_writes.plugin_root_render",
        group=GROUP,
        guard_ids=("hooks-integrations-plugin-root-render",),
        description="MCP/LSP plugin roots stay authored in recorded state and expand per target.",
        check=_check_plugin_root_render,
    ),
    Rule(
        id="mutation_writes.mcp_package_launcher",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-package-launcher",),
        description="MCP launcher selection and argv shape must route through MCPClientAdapter.",
        check=_check_mcp_package_launcher,
    ),
    Rule(
        id="mutation_writes.mcp_passthrough_denylist",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-passthrough-denylist",),
        description="MCP passthrough filtering must route through the base adapter denylist.",
        check=_check_mcp_passthrough_denylist,
    ),
    Rule(
        id="mutation_writes.opencode_enabled_intent",
        group=GROUP,
        guard_ids=("hooks-integrations-opencode-enabled-intent",),
        description="OpenCode enabled presence and trust must route through the MCP model.",
        check=_check_opencode_enabled_intent,
    ),
    Rule(
        id="mutation_writes.jetbrains_mcp_path",
        group=GROUP,
        guard_ids=("hooks-integrations-jetbrains-mcp-path",),
        description="JetBrains Copilot MCP config paths must come from the IntelliJ adapter.",
        check=_check_jetbrains_mcp_path,
    ),
    Rule(
        id="mutation_writes.mcp_json_config_write",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-json-config-write",),
        description="MCP JSON config merge-writes must route through the base adapter owner.",
        check=_check_mcp_json_config_write,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
