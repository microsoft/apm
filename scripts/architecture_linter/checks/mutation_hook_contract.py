"""Neutral-hook-contract and hook-command-vocabulary mutation analyzers.

Ports three of the owner guards recorded in
``.apm/architecture/owners/hooks-integrations.json``:

* ``hooks-integrations-copilot-cli-mcp-paths`` -- AC1 Copilot CLI MCP paths
  owner.
* ``hooks-integrations-neutral-hook-contract`` -- AC2/AC6/AC15c/AC4 hook
  grammar, per-target shape, validate-before-write ordering, drift
  projection (incl. ``check_hook_file_routing_owner`` and
  ``check_hook_config_write_owner`` ported semantically).
* ``hooks-integrations-hook-command-vocabulary`` -- AC15d plugin-root hook
  command parsing owner.

The two semantic (non-lexical) legacy helpers in this domain --
``check_hook_config_write_owner.py`` (write-mutating calls to merge-hook
config paths, with one-hop alias resolution) and the drift-projection
membership check -- are ported here as private ``_nhc_*`` helper functions
consumed only by :func:`_check_neutral_hook_contract`.

Sibling module :mod:`mutation_write_shared` carries the generic span/grep/
scan helpers every mutation-write check family shares.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scripts.architecture_linter.checks.mutation_write_shared import (
    _SRC,
    GROUP,
    _count_regex_lines,
    _duplicate_scan,
    _has_fixed,
    _has_regex,
    _python_paths,
    _read_required,
    _require,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    line_pattern_violations,
    violation,
)
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_INTEGRATION = "src/apm_cli/integration/"


_COPILOT_OWNER = "src/apm_cli/adapters/client/copilot.py"


_MCP_INTEGRATOR = "src/apm_cli/integration/mcp_integrator.py"


_HOOK_INTEGRATOR = "src/apm_cli/integration/hook_integrator.py"


_KIRO_HOOK_INTEGRATOR = "src/apm_cli/integration/kiro_hook_integrator.py"


_HOOK_CONTRACT = "src/apm_cli/hook_contract.py"


_HOOK_OWNERSHIP = "src/apm_cli/integration/hook_ownership.py"


_HOOK_COMMAND_PATHS = "src/apm_cli/integration/hook_command_paths.py"


_AGENT_PLUGIN_LOADER = "src/apm_cli/agent_plugins/loader.py"


_INSTALL_DRIFT = "src/apm_cli/install/drift.py"


_HOOK_FILE_ROUTING_TARGETS: tuple[str, ...] = (_HOOK_INTEGRATOR, _KIRO_HOOK_INTEGRATOR)


_MERGE_HOOK_ROOT_DIRS: frozenset[str] = frozenset(
    {".claude", ".codex", ".cursor", ".gemini", ".windsurf", ".antigravity"}
)


_MERGE_HOOK_FILENAMES: frozenset[str] = frozenset({"hooks.json", "settings.json"})


_SIDECAR_FILENAME_FRAGMENT = "apm-hooks.json"


_MUTATING_OPEN_MODE_CHARS: frozenset[str] = frozenset("waX+")


_WRITE_METHOD_NAMES: frozenset[str] = frozenset({"write_text", "write_bytes", "unlink"})


_MODULE_SCOPE = "<module>"


@dataclass(frozen=True)
class _HookConfigWrite:
    """A write-mutating call to a merge-hook config or sidecar path."""

    line: int
    qualname: str


@dataclass(frozen=True)
class _HookRouting:
    """A ``dep_targets_active`` gate wrapping ``_filter_hook_files_for_target``."""

    line: int


def _string_constants(index: TreeIndex, node: ast.AST) -> set[str]:
    """Collect every string-constant fragment in `node`'s subtree."""
    return {
        descendant.value
        for descendant in index.walk(node)
        if isinstance(descendant, ast.Constant) and isinstance(descendant.value, str)
    }


def _mode_is_mutating(mode: ast.AST | None) -> bool:
    """Return whether an ``open`` mode argument (or its absence) mutates."""
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(char in _MUTATING_OPEN_MODE_CHARS for char in mode.value)
    return True


def _open_call_mode_arg(node: ast.Call, *, mode_positional_index: int) -> ast.AST | None:
    """Return the mode argument of an ``open``-shaped call, positional or kw."""
    if len(node.args) > mode_positional_index:
        return node.args[mode_positional_index]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return keyword.value
    return None


def _mutating_path_operand(node: ast.AST) -> ast.AST | None:
    """Return the path operand of a write-mutating call, or ``None``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        if not node.args:
            return None
        if not _mode_is_mutating(_open_call_mode_arg(node, mode_positional_index=1)):
            return None
        return node.args[0]
    if isinstance(func, ast.Attribute):
        if func.attr == "open":
            if not _mode_is_mutating(_open_call_mode_arg(node, mode_positional_index=0)):
                return None
            return func.value
        if func.attr in _WRITE_METHOD_NAMES:
            return func.value
    return None


def _path_alias_fragments(index: TreeIndex, scope_nodes: Sequence[ast.AST]) -> dict[str, set[str]]:
    """Map ``name = <path-expr>`` locals to their string fragments (one hop)."""
    aliases: dict[str, set[str]] = {}
    for node in scope_nodes:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            aliases[target.id] = _string_constants(index, node.value)
    return aliases


def _resolve_operand_fragments(
    index: TreeIndex, operand: ast.AST, aliases: dict[str, set[str]]
) -> set[str]:
    """Resolve a call's path operand to its string-constant fragments."""
    if isinstance(operand, ast.Name) and operand.id in aliases:
        return aliases[operand.id]
    return _string_constants(index, operand)


def _is_hook_config_path(fragments: set[str]) -> bool:
    """Return whether fragments identify a merge-hook config or sidecar path."""
    if any(_SIDECAR_FILENAME_FRAGMENT in fragment for fragment in fragments):
        return True
    has_root_dir = any(fragment in _MERGE_HOOK_ROOT_DIRS for fragment in fragments)
    has_filename = any(fragment in _MERGE_HOOK_FILENAMES for fragment in fragments)
    return has_root_dir and has_filename


def _references_name(index: TreeIndex, node: ast.AST, name: str) -> bool:
    """Return whether `node`'s subtree references an ``ast.Name`` `name`."""
    return any(isinstance(child, ast.Name) and child.id == name for child in index.walk(node))


def _calls_named_function(index: TreeIndex, node: ast.AST, name: str) -> bool:
    """Return whether `node`'s subtree calls a bare function `name`."""
    return any(
        isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == name
        for child in index.walk(node)
    )


def _is_dependency_gated_routing(index: TreeIndex, node: ast.If) -> bool:
    """Return whether an ``if`` gates hook-file routing on dep targets."""
    return _references_name(index, node.test, "dep_targets_active") and _calls_named_function(
        index, node, "_filter_hook_files_for_target"
    )


def _scope_qualname(index: TreeIndex, node: ast.AST) -> str:
    """Return a function's ``Enclosing.name`` qualname, module scope aside.

    Mirrors the composite traversal's scope stack: the immediately enclosing
    ``def``/``class`` name is the prefix, and a module-level definition is
    reported bare.
    """
    anchor = index.definition_anchor(node)
    return node.name if anchor is None else f"{anchor.name}.{node.name}"


def _scope_writes(
    index: TreeIndex, scope_nodes: Sequence[ast.AST], qualname: str
) -> list[_HookConfigWrite]:
    """Detect merge-hook config writes within one own-scope."""
    aliases = _path_alias_fragments(index, scope_nodes)
    findings: list[_HookConfigWrite] = []
    for node in scope_nodes:
        operand = _mutating_path_operand(node)
        if operand is None:
            continue
        fragments = _resolve_operand_fragments(index, operand, aliases)
        if _is_hook_config_path(fragments):
            findings.append(_HookConfigWrite(line=node.lineno, qualname=qualname))
    return findings


def _hook_findings(index: TreeIndex | None) -> tuple[object, ...]:
    """Derive hook write and routing findings for one file, after traversal.

    Emission order matches the pre-order the shared traversal itself would
    have produced: module-scope writes first (module statements are judged on
    the module's direct children only, exactly as the legacy helper's implicit
    module scope was), then each definition's own-scope writes and each
    dependency-gated routing branch in source order.
    """
    if index is None or index.root is None:
        return ()
    findings: list[object] = []
    for node in index.nodes:
        if isinstance(node, ast.Module):
            findings.extend(_scope_writes(index, index.children(node), _MODULE_SCOPE))
        elif isinstance(node, FUNCTION_NODES):
            findings.extend(
                _scope_writes(index, index.own_scope(node), _scope_qualname(index, node))
            )
        if isinstance(node, ast.If) and _is_dependency_gated_routing(index, node):
            findings.append(_HookRouting(line=node.lineno))
    return tuple(findings)


def _count_fixed_lines(facts: FileFacts, needle: str) -> int:
    """Count lexical lines containing `needle` (mirrors ``grep -Fc``)."""
    return sum(1 for line in facts.lines if needle in line)


def _last_line_matching(facts: FileFacts, needle: str) -> int | None:
    """Return the last 1-based line containing fixed `needle`, or ``None``."""
    found: int | None = None
    for number, text in enumerate(facts.lines, start=1):
        if needle in text:
            found = number
    return found


def _first_line_after(facts: FileFacts, needle: str, *, after: int) -> int | None:
    """Return the first 1-based line > `after` containing `needle`."""
    for number, text in enumerate(facts.lines, start=1):
        if number > after and needle in text:
            return number
    return None


def _check_copilot_cli_mcp_paths(provider: FactsProvider) -> Iterable[Violation]:
    """Copilot CLI MCP config paths must come from the Copilot adapter.

    Ports the AC1 guard: the Copilot adapter owns ``COPILOT_HOME``, the MCP
    integrator constructs clients through ``ClientFactory.create_client``, and
    no other module hard-codes ``.github/mcp.json`` / ``mcp-config.json``.
    """
    rule_id = "mutation_writes.copilot_cli_mcp_paths"
    facts_by_path, failures = _read_required(provider, rule_id, (_COPILOT_OWNER, _MCP_INTEGRATOR))
    findings: list[Violation] = list(failures)
    if not failures:
        findings.extend(
            _require(
                _has_fixed(facts_by_path[_COPILOT_OWNER], "COPILOT_HOME"),
                rule_id,
                _COPILOT_OWNER,
                "Copilot adapter must own the COPILOT_HOME MCP config root",
            )
        )
        findings.extend(
            _require(
                _has_fixed(facts_by_path[_MCP_INTEGRATOR], "ClientFactory.create_client("),
                rule_id,
                _MCP_INTEGRATOR,
                "MCP integrator must build Copilot clients via ClientFactory.create_client",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_COPILOT_OWNER,)),
            pattern=r"\.github/mcp\.json|mcp-config\.json",
            message="Copilot CLI MCP paths must come from the Copilot adapter",
            exempt=True,
        )
    )
    return findings


def _check_neutral_hook_contract(provider: FactsProvider) -> Iterable[Violation]:
    """Neutral hook source grammar, per-target shape, and drift projection.

    Combines every ``HookIntegrator``/``hook_contract``/``hook_ownership``
    authority the shell enforced under AC2, AC6, AC15, AC15c, and AC4 into
    the single canonical owner guard: validation must precede the native
    write, rewrite scope and Claude project-dir and event-map ownership stay
    single-owner, the neutral IR carries no target vocabulary, hook command
    keys route through ``hook_contract``, per-file routing is not gated by
    dependency targets, merge-hook config writes stay owned by
    ``HookIntegrator``, ownership markers route through ``hook_ownership``,
    and shared drift projection routes through ``hook_ownership``.
    """
    rule_id = "mutation_writes.neutral_hook_contract"
    return (
        *_nhc_validation_before_write(provider, rule_id),
        *_nhc_rewrite_scope(provider, rule_id),
        *_nhc_claude_project_dir(provider, rule_id),
        *_nhc_event_map(provider, rule_id),
        *_nhc_contract_vocabulary(provider, rule_id),
        *_nhc_command_keys(provider, rule_id),
        *_nhc_file_routing(provider, rule_id),
        *_nhc_config_writes(provider, rule_id),
        *_nhc_ownership_markers(provider, rule_id),
        *_nhc_drift_projection(provider, rule_id),
    )


def _nhc_validation_before_write(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Hook payload validation must continue before the native payload write."""
    facts, failures = checked_facts(provider, _HOOK_INTEGRATOR, rule_id, require_python=True)
    if failures:
        return failures
    validation_line = _last_line_matching(facts, "if not validation.valid:")
    write_line = _last_line_matching(facts, 'with open(target_path, "w"')
    continue_line = (
        _first_line_after(facts, "continue", after=validation_line)
        if validation_line is not None
        else None
    )
    if (
        validation_line is None
        or continue_line is None
        or write_line is None
        or continue_line > write_line
    ):
        return (
            violation(
                rule_id,
                _HOOK_INTEGRATOR,
                "hook payload validation must continue before the native payload write",
            ),
        )
    return ()


def _nhc_rewrite_scope(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Hook rewrite scope must route through ``HookIntegrator``."""
    facts_by_path, failures = _read_required(
        provider, rule_id, (_HOOK_INTEGRATOR, _KIRO_HOOK_INTEGRATOR)
    )
    findings: list[Violation] = list(failures)
    if not failures:
        owner = facts_by_path[_HOOK_INTEGRATOR]
        findings.extend(
            _require(
                _count_regex_lines(owner, r"^    def _deploy_root_for_hook_rewrite\(") == 1,
                rule_id,
                _HOOK_INTEGRATOR,
                "_deploy_root_for_hook_rewrite must be owned once by HookIntegrator",
            )
        )
        findings.extend(
            _require(
                _has_fixed(
                    facts_by_path[_KIRO_HOOK_INTEGRATOR],
                    "deploy_root_for_rewrite = integrator._deploy_root_for_hook_rewrite",
                ),
                rule_id,
                _KIRO_HOOK_INTEGRATOR,
                "Kiro hook integrator must delegate rewrite scope to HookIntegrator",
            )
        )
    dup_paths = tuple(
        path
        for path in _python_paths(provider, under=_INTEGRATION, exclude=(_HOOK_INTEGRATOR,))
        if path.endswith("hook_integrator.py")
    )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=dup_paths,
            pattern=r"deploy_root_for_rewrite\s*=.*user_scope",
            message="hook rewrite scope must route through HookIntegrator",
            exempt=False,
            exclude_line_pattern=r"integrator\._deploy_root_for_hook_rewrite",
        )
    )
    return tuple(findings)


def _nhc_claude_project_dir(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Claude project hook paths must be owned once by ``HookIntegrator``."""
    facts, failures = checked_facts(provider, _HOOK_INTEGRATOR, rule_id, require_python=True)
    findings: list[Violation] = list(failures)
    if not failures:
        findings.extend(
            _require(
                _count_fixed_lines(facts, '"CLAUDE_PROJECT_DIR"') == 1,
                rule_id,
                _HOOK_INTEGRATOR,
                "CLAUDE_PROJECT_DIR must be owned exactly once by HookIntegrator",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_HOOK_INTEGRATOR,)),
            pattern=r'"CLAUDE_PROJECT_DIR"',
            message="Claude project hook paths must be owned by HookIntegrator",
            exempt=True,
        )
    )
    return tuple(findings)


def _nhc_event_map(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Native hook event mapping must have one ``HookIntegrator`` owner."""
    facts, failures = checked_facts(provider, _HOOK_INTEGRATOR, rule_id, require_python=True)
    findings: list[Violation] = list(failures)
    if not failures:
        findings.extend(
            _require(
                _count_regex_lines(facts, r"^_HOOK_EVENT_MAP\s*[:=]") == 1,
                rule_id,
                _HOOK_INTEGRATOR,
                "_HOOK_EVENT_MAP must be defined exactly once by HookIntegrator",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_HOOK_INTEGRATOR,)),
            pattern=r"^_HOOK_EVENT_MAP\s*[:=]",
            message="native hook event mapping must have one HookIntegrator owner",
            exempt=False,
        )
    )
    return tuple(findings)


def _nhc_contract_vocabulary(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Neutral hook IR must not carry target-renderer vocabulary."""
    return tuple(
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=(_HOOK_CONTRACT,),
            pattern=r"copilot|gemini|antigravity",
            message="neutral hook IR must not contain target-renderer vocabulary",
            exempt_marker=EXEMPT_MARKER,
        )
    )


def _nhc_command_keys(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Neutral hook source grammar must route through ``hook_contract.py``."""
    owner_count = sum(
        1
        for path in _python_paths(provider, under=_SRC)
        if _has_regex(provider.file_facts(path), r"^HOOK_COMMAND_KEYS: tuple")
    )
    facts_by_path, failures = _read_required(
        provider, rule_id, (_HOOK_CONTRACT, _AGENT_PLUGIN_LOADER)
    )
    findings: list[Violation] = list(failures)
    findings.extend(
        _require(
            owner_count == 1,
            rule_id,
            _HOOK_CONTRACT,
            "HOOK_COMMAND_KEYS must be declared by exactly one owner",
        )
    )
    if not failures:
        findings.extend(
            _require(
                _has_regex(facts_by_path[_HOOK_CONTRACT], r"^HOOK_COMMAND_KEYS: tuple"),
                rule_id,
                _HOOK_CONTRACT,
                "hook_contract.py must declare HOOK_COMMAND_KEYS",
            )
        )
        findings.extend(
            _require(
                not _has_fixed(facts_by_path[_AGENT_PLUGIN_LOADER], "integration.hook_integrator"),
                rule_id,
                _AGENT_PLUGIN_LOADER,
                "agent plugin loader must not import integration.hook_integrator",
            )
        )
    return tuple(findings)


def _nhc_file_routing(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Per-file hook routing must not be gated by ``dep_targets_active``."""
    findings: list[Violation] = []
    for path in _HOOK_FILE_ROUTING_TARGETS:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        for finding in _hook_findings(provider.tree_index(path)):
            if not isinstance(finding, _HookRouting):
                continue
            if _exempt_line(facts, finding.line):
                continue
            findings.append(
                violation(
                    rule_id,
                    path,
                    "dep_targets_active must not gate _filter_hook_files_for_target",
                    line=finding.line,
                )
            )
    return tuple(findings)


def _nhc_config_writes(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Merge-hook config/sidecar writes must stay owned by ``HookIntegrator``."""
    findings: list[Violation] = []
    for path in _python_paths(provider, under=_SRC, exclude=(_HOOK_INTEGRATOR,)):
        _facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        for finding in _hook_findings(provider.tree_index(path)):
            if not isinstance(finding, _HookConfigWrite):
                continue
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"{finding.qualname} writes/deletes a merge-hook config or sidecar path "
                    "directly; this must stay owned by HookIntegrator "
                    "(src/apm_cli/integration/hook_integrator.py)",
                    line=finding.line,
                )
            )
    return tuple(findings)


def _nhc_ownership_markers(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Merged-hook ownership markers must route through ``hook_ownership.py``."""
    facts_by_path, failures = _read_required(provider, rule_id, (_HOOK_OWNERSHIP, _HOOK_INTEGRATOR))
    if failures:
        return failures
    owner = facts_by_path[_HOOK_OWNERSHIP]
    consumer = facts_by_path[_HOOK_INTEGRATOR]
    findings: list[Violation] = []
    findings.extend(
        _require(
            _has_regex(owner, r"^def dependency_hook_source_marker\("),
            rule_id,
            _HOOK_OWNERSHIP,
            "hook_ownership.py must define dependency_hook_source_marker",
        )
    )
    findings.extend(
        _require(
            _has_regex(owner, r"^def dependency_hook_sources\("),
            rule_id,
            _HOOK_OWNERSHIP,
            "hook_ownership.py must define dependency_hook_sources",
        )
    )
    findings.extend(
        _require(
            _has_fixed(consumer, "from apm_cli.integration.hook_ownership import ("),
            rule_id,
            _HOOK_INTEGRATOR,
            "HookIntegrator must import ownership markers from hook_ownership.py",
        )
    )
    findings.extend(
        _require(
            not _has_regex(consumer, r"^    def _dependency_hook_source"),
            rule_id,
            _HOOK_INTEGRATOR,
            "HookIntegrator must not reimplement a local dependency hook source marker",
        )
    )
    return tuple(findings)


def _nhc_drift_projection(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Shared hook drift projection must route through ``hook_ownership.py``."""
    facts_by_path, failures = _read_required(provider, rule_id, (_HOOK_OWNERSHIP, _INSTALL_DRIFT))
    findings: list[Violation] = list(failures)
    if not failures:
        findings.extend(
            _require(
                _count_regex_lines(
                    facts_by_path[_HOOK_OWNERSHIP], r"^def project_apm_owned_hook_entries\("
                )
                == 1,
                rule_id,
                _HOOK_OWNERSHIP,
                "project_apm_owned_hook_entries must be defined exactly once",
            )
        )
        findings.extend(
            _require(
                _count_fixed_lines(facts_by_path[_INSTALL_DRIFT], "project_apm_owned_hook_entries(")
                == 2,
                rule_id,
                _INSTALL_DRIFT,
                "install drift must call project_apm_owned_hook_entries exactly twice",
            )
        )
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_HOOK_OWNERSHIP,)),
            pattern=r"^def project_apm_owned_hook_entries\(",
            message="shared hook drift projection must route through hook_ownership.py",
            exempt=False,
        )
    )
    return tuple(findings)


def _exempt_line(facts: FileFacts, line: int) -> bool:
    """Return whether a 1-based line carries the authority exemption marker."""
    return 0 < line <= len(facts.lines) and EXEMPT_MARKER in facts.lines[line - 1]


def _check_hook_command_vocabulary(provider: FactsProvider) -> Iterable[Violation]:
    """Plugin-root hook command parsing must route through the owner.

    Ports the AC15d guard: ``hook_command_paths.py`` owns
    ``PLUGIN_ROOT_NAMES`` and the ``HookIntegrator`` consumer must not
    hard-code per-target plugin-root constants or the ``"PLUGIN_ROOT"``
    literal.
    """
    rule_id = "mutation_writes.hook_command_vocabulary"
    facts_by_path, failures = _read_required(
        provider, rule_id, (_HOOK_COMMAND_PATHS, _HOOK_INTEGRATOR)
    )
    if failures:
        return failures
    owner = facts_by_path[_HOOK_COMMAND_PATHS]
    consumer = facts_by_path[_HOOK_INTEGRATOR]
    findings: list[Violation] = []
    findings.extend(
        _require(
            _has_regex(owner, r"^PLUGIN_ROOT_NAMES = \("),
            rule_id,
            _HOOK_COMMAND_PATHS,
            "hook_command_paths.py must own PLUGIN_ROOT_NAMES",
        )
    )
    findings.extend(
        _require(
            not _has_regex(consumer, r"CLAUDE_PLUGIN_ROOT|CURSOR_PLUGIN_ROOT|KIRO_PLUGIN_ROOT"),
            rule_id,
            _HOOK_INTEGRATOR,
            "HookIntegrator must not hard-code per-target plugin-root constants",
        )
    )
    findings.extend(
        _require(
            not _has_fixed(consumer, '"PLUGIN_ROOT"'),
            rule_id,
            _HOOK_INTEGRATOR,
            "HookIntegrator must not hard-code the PLUGIN_ROOT literal",
        )
    )
    return findings


RULES: tuple[Rule, ...] = (
    Rule(
        id="mutation_writes.copilot_cli_mcp_paths",
        group=GROUP,
        guard_ids=("hooks-integrations-copilot-cli-mcp-paths",),
        description="Copilot CLI MCP config paths must come from the Copilot adapter.",
        check=_check_copilot_cli_mcp_paths,
    ),
    Rule(
        id="mutation_writes.neutral_hook_contract",
        group=GROUP,
        guard_ids=("hooks-integrations-neutral-hook-contract",),
        description="Neutral hook grammar, per-target shape, and drift projection stay owned.",
        check=_check_neutral_hook_contract,
    ),
    Rule(
        id="mutation_writes.hook_command_vocabulary",
        group=GROUP,
        guard_ids=("hooks-integrations-hook-command-vocabulary",),
        description="Plugin-root hook command parsing must route through hook_command_paths.py.",
        check=_check_hook_command_vocabulary,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
