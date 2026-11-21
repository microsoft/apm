"""MCP remote transport normalization and per-harness remote shape guards.

Ports two owner guards recorded in
``.apm/architecture/owners/hooks-integrations.json``:

* ``hooks-integrations-mcp-remote-transport-normalization`` -- adapters and
  ``apm mcp show`` read a remote's transport only through
  ``mcp_transport.canonical_remote_transport``; ingestion records it.
* ``hooks-integrations-mcp-remote-shape-table`` -- every runtime registered in
  ``ClientFactory`` binds its own row of ``adapters/client/_remote_shapes.py``
  in its class body, every row cites evidence, the unverified rows are
  listed explicitly, and an adapter whose row retires earlier shapes keeps
  native comparison on so those APM writes heal.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from scripts.architecture_linter.checks.mutation_write_shared import (
    GROUP,
    _duplicate_scan,
    _has_fixed,
    _python_paths,
    _read_required,
    _require,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_ADAPTERS = "src/apm_cli/adapters/"
_ADAPTERS_CLIENT = "src/apm_cli/adapters/client/"
_MCP_SHOW = "src/apm_cli/commands/mcp.py"
_OWNER = "src/apm_cli/mcp_transport.py"
_REGISTRY_CLIENT = "src/apm_cli/registry/client.py"
_INTEGRATOR = "src/apm_cli/integration/mcp_integrator.py"
_FACTORY = "src/apm_cli/factory.py"
_SHAPES = "src/apm_cli/adapters/client/_remote_shapes.py"

_RAW_TRANSPORT_READ = (
    r"[\"']transport_type[\"']"
    r"|remote\.get\(\s*[\"']type[\"']"
    r"|remote\[\s*[\"']type[\"']\s*\]"
)


def _check_remote_transport_normalization(provider: FactsProvider) -> Iterable[Violation]:
    """Remote transport is derived only by the T-norm owner."""
    rule_id = "mutation_writes.mcp_remote_transport_normalization"
    facts_by_path, failures = _read_required(
        provider, rule_id, (_OWNER, _REGISTRY_CLIENT, _INTEGRATOR, _MCP_SHOW)
    )
    findings: list[Violation] = list(failures)
    if failures:
        return tuple(findings)
    findings.extend(
        _require(
            _has_fixed(facts_by_path[_OWNER], "def canonical_remote_transport("),
            rule_id,
            _OWNER,
            "mcp_transport.py must own canonical_remote_transport",
        )
    )
    for path, needle, message in (
        (
            _REGISTRY_CLIENT,
            'normalized["transport"] = canonical_remote_transport(remote)',
            "registry ingestion must record each remote's canonical transport",
        ),
        (
            _INTEGRATOR,
            '"transport": canonical_remote_transport({"transport": dep.transport})',
            "self-defined remotes must record the canonical transport",
        ),
        (
            _MCP_SHOW,
            "return canonical_remote_transport(remote)",
            "apm mcp show must read remote transport through the owner",
        ),
    ):
        findings.extend(_require(_has_fixed(facts_by_path[path], needle), rule_id, path, message))
    findings.extend(
        _duplicate_scan(
            provider,
            rule_id=rule_id,
            paths=(*_python_paths(provider, under=_ADAPTERS), _MCP_SHOW),
            pattern=_RAW_TRANSPORT_READ,
            message=(
                "read a remote's transport through mcp_transport.canonical_remote_transport, "
                "not raw transport_type/type keys"
            ),
            exempt=False,
        )
    )
    return tuple(findings)


def _parse(provider: FactsProvider, path: str, rule_id: str) -> tuple[ast.Module | None, tuple]:
    facts_by_path, failures = _read_required(provider, rule_id, (path,))
    if failures:
        return None, failures
    try:
        return ast.parse(source_text(facts_by_path[path])), ()
    except SyntaxError as exc:
        return None, (violation(rule_id, path, f"unparsable source: {exc.msg}"),)


def _registered_adapters(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``runtime -> class name`` and ``class name -> module path``."""
    modules: dict[str, str] = {}
    registry: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 1:
            for alias in node.names:
                modules[alias.asname or alias.name] = (
                    "src/apm_cli/" + node.module.replace(".", "/") + ".py"
                )
        target = getattr(node, "target", None) or (
            node.targets[0] if isinstance(node, ast.Assign) and node.targets else None
        )
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and target.id == "_MCP_CLIENT_REGISTRY"
            and isinstance(value, ast.Dict)
        ):
            for key, item in zip(value.keys, value.values, strict=False):
                if isinstance(key, ast.Constant) and isinstance(item, ast.Name):
                    registry[str(key.value)] = item.id
    return registry, modules


def _binds_own_row(tree: ast.Module, class_name: str, runtime: str) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and [getattr(t, "id", None) for t in statement.targets] == ["remote_shape"]
                and isinstance(statement.value, ast.Call)
                and getattr(statement.value.func, "id", None) == "remote_shape_row"
                and [getattr(a, "value", None) for a in statement.value.args] == [runtime]
            ):
                return True
    return False


def _shape_rows(tree: ast.Module) -> tuple[dict[str, dict[str, ast.expr]], set[str] | None]:
    rows: dict[str, dict[str, ast.expr]] = {}
    unverified: set[str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "RemoteShape":
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            runtime = keywords.get("runtime")
            if isinstance(runtime, ast.Constant):
                rows[str(runtime.value)] = keywords
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", None) == "UNVERIFIED_REMOTE_SHAPES"
            and isinstance(node.value, ast.Call)
            and node.value.args
            and isinstance(node.value.args[0], ast.Set)
        ):
            unverified = {
                str(element.value)
                for element in node.value.args[0].elts
                if isinstance(element, ast.Constant)
            }
    return rows, unverified


def _opts_out_of_native_comparison(tree: ast.Module, class_name: str) -> bool:
    """Whether *class_name* sets ``compares_native_connections`` to False in its body."""
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            elif isinstance(statement, ast.Assign):
                targets = statement.targets
            else:
                continue
            if [getattr(t, "id", None) for t in targets] == ["compares_native_connections"] and (
                isinstance(statement.value, ast.Constant) and statement.value.value is False
            ):
                return True
    return False


def _has_retired_shapes(row: dict[str, ast.expr]) -> bool:
    retired = row.get("retired")
    return retired is not None and not (isinstance(retired, ast.Tuple) and not retired.elts)


def _check_remote_shape_table(provider: FactsProvider) -> Iterable[Violation]:
    """Each registered MCP runtime binds its own evidence-backed remote shape row."""
    rule_id = "mutation_writes.mcp_remote_shape_table"
    factory, failures = _parse(provider, _FACTORY, rule_id)
    shapes, shape_failures = _parse(provider, _SHAPES, rule_id)
    findings: list[Violation] = [*failures, *shape_failures]
    if factory is None or shapes is None:
        return tuple(findings)
    registry, modules = _registered_adapters(factory)
    if not registry:
        return (violation(rule_id, _FACTORY, "could not read _MCP_CLIENT_REGISTRY"),)
    rows, unverified = _shape_rows(shapes)
    for runtime, class_name in sorted(registry.items()):
        row = rows.get(runtime)
        if row is None:
            findings.append(violation(rule_id, _SHAPES, f"missing remote shape row for {runtime}"))
        elif "evidence_url" not in row:
            findings.append(violation(rule_id, _SHAPES, f"row {runtime} must cite evidence_url"))
        module = modules.get(class_name)
        tree, module_failures = _parse(provider, module, rule_id) if module else (None, ())
        findings.extend(module_failures)
        if tree is None or not _binds_own_row(tree, class_name, runtime):
            findings.append(
                violation(
                    rule_id,
                    module or _FACTORY,
                    f"{class_name} must bind remote_shape = remote_shape_row({runtime!r}) "
                    "in its own class body",
                )
            )
        elif row is not None and _has_retired_shapes(row):
            if _opts_out_of_native_comparison(tree, class_name):
                # The retired-shape heal runs only through native comparison
                # (install.mcp.ownership.native_comparison_clients).
                findings.append(
                    violation(
                        rule_id,
                        module,
                        f"{class_name} declares retired remote shapes but sets "
                        "compares_native_connections = False, so earlier APM writes never heal",
                    )
                )
    stale = set(rows) - set(registry)
    if stale:
        findings.append(
            violation(rule_id, _SHAPES, f"rows for unregistered runtimes: {sorted(stale)}")
        )
    declared_unverified = {
        runtime
        for runtime, row in rows.items()
        if isinstance(row.get("verified"), ast.Constant) and row["verified"].value is False
    }
    if unverified != declared_unverified:
        findings.append(
            violation(
                rule_id,
                _SHAPES,
                "UNVERIFIED_REMOTE_SHAPES must list exactly the verified=False rows: "
                f"{sorted(declared_unverified)}",
            )
        )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id="mutation_writes.mcp_remote_transport_normalization",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-remote-transport-normalization",),
        description="MCP remote transport is read only through mcp_transport.canonical_remote_transport.",
        check=_check_remote_transport_normalization,
    ),
    Rule(
        id="mutation_writes.mcp_remote_shape_table",
        group=GROUP,
        guard_ids=("hooks-integrations-mcp-remote-shape-table",),
        description="Every registered MCP runtime binds its own evidence-backed remote shape row.",
        check=_check_remote_shape_table,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
