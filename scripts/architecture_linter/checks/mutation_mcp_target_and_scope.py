"""MCP target-selection, declaration-scope, and launcher mutation analyzers.

Ports three of the owner guards recorded in
``.apm/architecture/owners/hooks-integrations.json``:

* ``hooks-integrations-mcp-target-selection`` -- AC21 MCP manifest target
  precedence.
* ``hooks-integrations-mcp-declaration-scope`` -- AC25 root vs dependency MCP
  scope.
* ``hooks-integrations-mcp-package-launcher`` -- AC26/AC30/AC32
  MCPClientAdapter launcher selection and argv shape.
* ``hooks-integrations-jetbrains-mcp-path`` -- AC28 JetBrains Copilot MCP
  path.
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


_APM_PACKAGE = "src/apm_cli/models/apm_package.py"


_MCP_SCOPE_OWNER = "src/apm_cli/integration/mcp_config_view.py"


_MCP_ADAPTER_BASE = "src/apm_cli/adapters/client/base.py"


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
    return findings


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
        provider, rule_id, (_MCP_INSTALL, _MCP_INTEGRATION, _APM_PACKAGE)
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
    if not _span_has_fixed(install_facts, adapter_span, "parse_targets_field(apm_config)"):
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
    if _span_has_fixed(install_facts, resolver_span, "parse_targets_field("):
        findings.append(
            violation(rule_id, _MCP_INSTALL, "resolver must not parse manifest targets itself")
        )
    validation_line = _first_span_line(
        integration_facts, integration_span, "parse_targets_field(mcp_apm_config)"
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
    return findings


def _count_span_fixed(facts: FileFacts, span: tuple[int, int], needle: str) -> int:
    """Count lines in `span` containing fixed `needle`."""
    return sum(1 for _, text in _span_lines(facts, span) if needle in text)


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


RULES: tuple[Rule, ...] = (
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
        id="mutation_writes.mcp_package_launcher",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-package-launcher",),
        description="MCP launcher selection and argv shape must route through MCPClientAdapter.",
        check=_check_mcp_package_launcher,
    ),
    Rule(
        id="mutation_writes.jetbrains_mcp_path",
        group=GROUP,
        guard_ids=("hooks-integrations-jetbrains-mcp-path",),
        description="JetBrains Copilot MCP config paths must come from the IntelliJ adapter.",
        check=_check_jetbrains_mcp_path,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
