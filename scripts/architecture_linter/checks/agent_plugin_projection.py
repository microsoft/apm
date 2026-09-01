"""Agent Plugin projection and native-deployment-boundary AST analyzer.

A faithful, facts-only port of ``scripts/check_agent_plugin_projection_boundary.py``
(legacy bundle-format subcheck **B20**), the 44 KB AST guard that keeps the
Agent Plugin compatibility projection and the native deployment boundary from
splitting into parallel authorities.

The analyzer never parses or walks source itself. The one shared composite
traversal in :mod:`scripts.architecture_linter.facts` already retains every
file's ``(node, parent)`` records intrinsically, and the canonical
:func:`~scripts.architecture_linter.checks.tree_index.build_tree_index` folds those
records into child and descendant adjacency, so every ``ast.walk(x)`` in the
legacy helper becomes an ``index.walk(x)`` over pre-indexed descendants. Every
other question (module-wide call terminals, per-function call sets, imports,
definition counts across the whole ``src/apm_cli`` tree) is answered from the
cached :class:`~scripts.architecture_linter.models.FileFacts` the same shared
traversal already produced.

Fail-closed behavior matches the legacy helper: a missing required owner file
short-circuits with one violation per missing path, and an unreadable or
unparseable source is reported rather than skipped.

The individual ``_check_*(ctx)`` boundary checks this entry point composes
live in sibling modules (:mod:`agent_plugin_boundary_checks_a`,
:mod:`agent_plugin_boundary_checks_b`, :mod:`agent_plugin_boundary_checks_c`)
purely so no single module outgrows the module size budget; the low-level
scan primitives live in :mod:`agent_plugin_scan_primitives`.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from scripts.architecture_linter.checks.agent_plugin_boundary_checks_a import (
    _check_deployment_boundary_owner,
    _check_first_action_gate,
    _check_integration_template,
    _check_survivor_preflight_owner,
)
from scripts.architecture_linter.checks.agent_plugin_boundary_checks_b import (
    _check_drift_translation,
    _check_hook_reconciliation,
    _check_install_command,
    _check_integrate_phase,
    _check_local_bundle_handler,
    _check_prune_survivor_preflight,
    _check_uninstall_command,
    _check_uninstall_survivor_preflight,
)
from scripts.architecture_linter.checks.agent_plugin_boundary_checks_c import (
    _check_package_owner,
    _check_projection_owner,
    _check_resolver_owner,
    _check_skill_integration,
    _check_validation_owner,
)
from scripts.architecture_linter.checks.agent_plugin_scan_primitives import _Boundary
from scripts.architecture_linter.checks.agent_plugin_shared import (
    CI_CHECKS,
    ERRORS,
    HOOK_INTEGRATOR,
    INSTALL_COMMAND,
    INTEGRATE_PHASE,
    LOCAL_BUNDLE_HANDLER,
    PACKAGE,
    PROJECTION,
    PRUNE_COMMAND,
    RESOLVER,
    SKILL_INTEGRATOR,
    SKILL_ROUTING,
    TEMPLATE,
    UNINSTALL_CLI,
    UNINSTALL_ENGINE,
    VALIDATION,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import FileFacts, Violation

_SENTINEL = "pyproject.toml"


_SRC_PREFIX = "src/apm_cli/"


_PY: tuple[str, ...] = (".py",)


SERVICES = "src/apm_cli/install/services.py"


CAPABILITY = "src/apm_cli/copilot_plugins/capability.py"


DRIFT = "src/apm_cli/install/drift.py"


REQUIRED_PATHS: tuple[str, ...] = (
    PROJECTION,
    PACKAGE,
    VALIDATION,
    RESOLVER,
    ERRORS,
    SERVICES,
    TEMPLATE,
    INTEGRATE_PHASE,
    LOCAL_BUNDLE_HANDLER,
    CI_CHECKS,
    UNINSTALL_CLI,
    UNINSTALL_ENGINE,
    INSTALL_COMMAND,
    PRUNE_COMMAND,
    HOOK_INTEGRATOR,
    SKILL_INTEGRATOR,
    SKILL_ROUTING,
)


_RAW_READER_CALLS = frozenset(
    {
        "json.load",
        "json.loads",
        "load_yaml",
        "read_json_document",
        "yaml.load",
        "yaml.safe_load",
    }
)


_ADMISSION_CALL_NAMES = frozenset(
    {
        "activate_native_registration",
        "native_registration_scope",
        "resolve_native_registration_capability",
    }
)


_FORBIDDEN_DISCOVERY_MODULES = frozenset({"subprocess", "shutil"})


_FORBIDDEN_DISCOVERY_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
        "shutil.which",
        "os.system",
    }
)


_CAPABILITY_OWNER_DEF = "resolve_native_registration_capability"


def _dotted_callee(qualname: str) -> str:
    """Reconstruct the helper's ``_call_name`` from an unparsed callee expression.

    ``_call_name`` collects the trailing attribute chain and prepends the base
    only when the base is a plain ``Name``. The unparsed callee renders exactly
    that chain as a dot-joined suffix of identifier segments, so the maximal
    trailing run of identifier segments is the same string -- ``a[0].b.c``
    yields ``b.c`` and ``self.foo.bar`` yields ``self.foo.bar``.
    """
    segments = qualname.split(".")
    index = len(segments)
    while index > 0 and segments[index - 1].isidentifier():
        index -= 1
    return ".".join(segments[index:])


def _function_spans(facts: FileFacts) -> tuple[tuple[str, int, int], ...]:
    """Every function definition as ``(name, first_line, last_line)``."""
    return tuple(
        (definition.name, definition.line, definition.end_line)
        for definition in facts.definitions
        if definition.kind in ("function", "async_function")
    )


@dataclass(frozen=True)
class _CallLineIndex:
    """One file's calls, dotted once and sorted once, for O(log n) span queries."""

    lines: tuple[int, ...]
    names: tuple[str, ...]

    def in_span(self, low: int, high: int) -> set[str]:
        """Dotted callees of every call inside the inclusive line span."""
        start = bisect_left(self.lines, low)
        end = bisect_right(self.lines, high)
        return set(self.names[start:end])

    def all_names(self) -> set[str]:
        """Every dotted callee in the file."""
        return set(self.names)


def _call_line_index(facts: FileFacts) -> _CallLineIndex:
    """Dot-normalize and line-sort a file's calls exactly once."""
    ordered = sorted((call.line, _dotted_callee(call.qualname)) for call in facts.calls)
    return _CallLineIndex(
        lines=tuple(line for line, _ in ordered),
        names=tuple(name for _, name in ordered),
    )


def _scan_source_tree(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Repository-wide halves of the boundary: normalization, raw reads, discovery."""
    findings: list[Violation] = []
    admission_paths: set[str] = {CAPABILITY}
    capability_owners: list[tuple[str, int]] = []
    readable: dict[str, FileFacts] = {}
    call_index: dict[str, _CallLineIndex] = {}

    for path in inventory_paths(provider, prefixes=(_SRC_PREFIX,)):
        if not path.endswith(_PY):
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        readable[path] = facts
        calls = _call_line_index(facts)
        call_index[path] = calls
        module_calls = calls.all_names()
        if {call.rsplit(".", 1)[-1] for call in module_calls} & _ADMISSION_CALL_NAMES:
            admission_paths.add(path)
        capability_owners.extend(
            (path, name_line)
            for name, name_line, _end in _function_spans(facts)
            if name == _CAPABILITY_OWNER_DEF
        )
        findings.extend(_scan_function_boundaries(rule_id, path, facts, calls))

    findings.extend(_scan_admission_lifecycle(rule_id, admission_paths, readable, call_index))
    if len(capability_owners) != 1 or capability_owners[0][0] != CAPABILITY:
        findings.append(
            violation(
                rule_id,
                CAPABILITY,
                f"{_CAPABILITY_OWNER_DEF} must have exactly one definition, owned by capability.py",
            )
        )
    return tuple(findings)


def _scan_function_boundaries(
    rule_id: str, path: str, facts: FileFacts, call_index: _CallLineIndex
) -> tuple[Violation, ...]:
    """Per-function normalization-owner and raw-reader bans for one source file."""
    findings: list[Violation] = []
    for name, low, high in _function_spans(facts):
        calls = call_index.in_span(low, high)
        legacy_normalization_owner = (
            path == VALIDATION and name == "_validate_marketplace_plugin"
        ) or (
            path == DRIFT
            and name == "_normalize_legacy_local_plugin_for_replay"
            and "detect_agent_plugin" in calls
        )
        if "normalize_plugin_directory" in calls and not legacy_normalization_owner:
            findings.append(
                violation(
                    rule_id,
                    path,
                    "Claude normalization call outside _validate_marketplace_plugin",
                    line=low,
                )
            )
        if "APMPackage" in calls and calls & _RAW_READER_CALLS and path != PROJECTION:
            findings.append(
                violation(rule_id, path, "raw document parsing constructs APMPackage", line=low)
            )
    return tuple(findings)


def _scan_admission_lifecycle(
    rule_id: str,
    admission_paths: set[str],
    readable: dict[str, FileFacts],
    call_index: dict[str, _CallLineIndex],
) -> tuple[Violation, ...]:
    """No Copilot binary/version discovery anywhere in the admission lifecycle."""
    findings: list[Violation] = []
    for path in sorted(admission_paths):
        facts = readable.get(path)
        calls = call_index.get(path)
        if facts is None or calls is None:
            findings.append(violation(rule_id, path, "required owner file is missing"))
            continue
        for record in facts.imports:
            findings.extend(_forbidden_discovery_import(rule_id, path, record))
        forbidden_hits = calls.all_names() & _FORBIDDEN_DISCOVERY_CALLS
        if forbidden_hits:
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"must not call {sorted(forbidden_hits)} (no Copilot binary/version discovery)",
                )
            )
    return tuple(findings)


def _forbidden_discovery_import(rule_id: str, path: str, record: object) -> tuple[Violation, ...]:
    """One import statement's contribution to the discovery ban."""
    module = getattr(record, "module", None)
    level = getattr(record, "level", 0)
    names = getattr(record, "names", ())
    line = getattr(record, "line", 1)
    if module is None and level == 0:
        return tuple(
            violation(
                rule_id,
                path,
                f"must not import {name} (no Copilot binary/version discovery)",
                line=line,
            )
            for name in names
            if name.split(".")[0] in _FORBIDDEN_DISCOVERY_MODULES
        )
    module_root = (module or "").split(".")[0]
    if module_root in _FORBIDDEN_DISCOVERY_MODULES:
        return (
            violation(
                rule_id,
                path,
                f"must not import from {module} (no Copilot binary/version discovery)",
                line=line,
            ),
        )
    return ()


def check_projection_boundary(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Run the full legacy B20 projection/deployment-boundary AST guard."""
    inv = frozenset(provider.inventory)
    missing = [path for path in REQUIRED_PATHS if path not in inv]
    if missing:
        return tuple(
            violation(rule_id, _SENTINEL, f"required owner file is missing: {path}")
            for path in missing
        )

    findings: list[Violation] = []
    trees: dict[str, TreeIndex] = {}
    for path in REQUIRED_PATHS:
        _facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        index = provider.tree_index(path)
        if index is None or index.root is None:
            findings.append(
                violation(
                    rule_id, path, "shared traversal produced no tree for a required owner file"
                )
            )
            continue
        trees[path] = index
    if len(trees) != len(REQUIRED_PATHS):
        return tuple(findings)

    ctx = _Boundary(rule_id=rule_id, trees=trees)
    findings.extend(_check_deployment_boundary_owner(ctx))
    findings.extend(
        _check_first_action_gate(
            ctx,
            SERVICES,
            "integrate_package_primitives",
            "native deployment gate must be the first integration action",
        )
    )
    findings.extend(_check_survivor_preflight_owner(ctx))
    findings.extend(_check_integration_template(ctx))
    findings.extend(_check_integrate_phase(ctx))
    findings.extend(_check_install_command(ctx))
    findings.extend(
        _check_first_action_gate(
            ctx,
            SERVICES,
            "integrate_local_bundle",
            "opaque local bundle deployment must start at the native boundary",
        )
    )
    findings.extend(_check_local_bundle_handler(ctx))
    findings.extend(_check_drift_translation(ctx))
    findings.extend(_check_uninstall_survivor_preflight(ctx))
    findings.extend(_check_prune_survivor_preflight(ctx))
    findings.extend(_check_hook_reconciliation(ctx))
    findings.extend(_check_uninstall_command(ctx))
    findings.extend(_check_skill_integration(ctx))
    findings.extend(_check_projection_owner(ctx))
    findings.extend(_check_package_owner(ctx))
    findings.extend(_check_validation_owner(ctx))
    findings.extend(_check_resolver_owner(ctx))
    findings.extend(_scan_source_tree(provider, rule_id))
    return tuple(findings)


__all__ = ["check_projection_boundary"]
