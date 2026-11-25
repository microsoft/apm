"""A managed file is replaced only with deployment evidence (#4360, #4626).

``BaseIntegrator.keeps_unproven_file`` owns the overwrite decision for
instructions, agents, prompts, commands and rules (through
``_check_adopt_or_skip``) and for hook files, copied hook scripts and canvas
files (through ``keeps_edited_output``). The guard is closed-world:

* the owner has exactly one write-permitting ``return False``, and it is
  reached only through ``force``, a missing file, or the install-bound
  ``OverwriteGate.authorizes`` check; both adapters ask it and
  ``_check_adopt_or_skip`` has no write-permitting ``return False, False``;
* ``DeploymentMaterializationSession.overwrite_authorized`` decides from a
  trusted receipt (``_trusted_hash`` with ``_write_locator``) or the locked
  render verdict, never from lockfile hashes;
* ``recorded_writes`` binds the gate around every integrator call;
* in every file-primitive integrator, a function that writes bytes asks the
  owner itself, or is one of the listed leaf writers whose in-module callers
  all ask the owner;
* in every executable writer module (Copilot and Kiro hook files, the hook
  script copy, the canvas bundle), every write is control-dependent on the
  gate's result: it sits in the body of ``if not <gate>(...)``, or follows a
  sibling ``if <gate>(...):`` whose body leaves (``continue``/``return``).
  A short-circuited test, an ignored result or a new unguarded writer fails;
  only the listed non-executable writes (merged settings and sidecar, cleanup),
  each by operation and destination, are exempt; a gate must ask about the
  path the write replaces (itself, its resolved form or the judged member slot),
  bound by the binding that reaches the write; a write deferred in a lambda
  is never gated.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_OVERWRITE_EVIDENCE = "install-deployment-overwrite-evidence"
_BASE = "src/apm_cli/integration/base_integrator.py"
_LEDGER = "src/apm_cli/core/deployment_ledger.py"
_OWNER = "BaseIntegrator.keeps_unproven_file"
_ADAPTERS = ("BaseIntegrator._check_adopt_or_skip", "BaseIntegrator.keeps_edited_output")
_OWNER_CALL = "_check_adopt_or_skip"
_EDITED_CALL = "keeps_edited_output"
# Executable writer modules: every write in them is gated (#4626, #4936).
_EXECUTABLE_MODULES = (
    "src/apm_cli/integration/hook_integrator.py",
    "src/apm_cli/integration/hook_bundle.py",
    "src/apm_cli/integration/kiro_hook_integrator.py",
    "src/apm_cli/integration/canvas_integrator.py",
)
# Gate asks whose result a write must depend on.
_EXECUTABLE_GATES = frozenset({"keeps_edited_output", "_keeps_edited_bundle"})
# The exact (operation, destination) writes in those modules that never
# replace an executable output; any other write in the same function is gated.
_UNGATED_EXECUTABLE_WRITES = {
    "src/apm_cli/integration/hook_integrator.py": {
        # Merged hook entries in a shared settings file (the sidecar is
        # written by hook_ownership.write_hook_sidecar, #5009).
        "HookIntegrator._integrate_merged_hooks": frozenset(
            {("write_text_if_changed", "json_path")}
        ),
        # Removing APM entries from a shared settings file.
        "HookIntegrator._clean_apm_source_from_json": frozenset(
            {("atomic_write_text", "json_path")}
        ),
        # Exactly this one call (#5181): the cleaned JSON replaces the config through
        # write_seam.atomic_write_text inside provenance.recorded_config_write, whose
        # record (#5009) and the seam's journal hash (#4863) are the overwrite evidence.
        "HookIntegrator._clean_apm_entries_from_json": frozenset(
            {("atomic_write_text", "json_path")}
        ),
    },
}
_CANVAS_GATE = (
    "src/apm_cli/integration/canvas_integrator.py",
    "CanvasIntegrator._keeps_edited_bundle",
)
_AUTHORIZED = "DeploymentMaterializationSession.overwrite_authorized"
_GATE = "OverwriteGate.authorizes"
_WRITERS = (
    "src/apm_cli/integration/instruction_integrator.py",
    "src/apm_cli/integration/prompt_integrator.py",
    "src/apm_cli/integration/command_integrator.py",
    "src/apm_cli/integration/agent_integrator.py",
)
_RAW_WRITES = frozenset(
    {
        "write_text_lf",
        "write_text_if_changed",
        "write_text",
        "write_bytes",
        "copy2",
        "copyfile",
        "open",
    }
)
# Content writes in an executable writer module (``open`` only in a write mode).
_EXECUTABLE_WRITES = (_RAW_WRITES - {"open"}) | {
    "atomic_write_text",
    "copytree",
    "move",
    "open_for_write",
    "rename",
    "write_json_config",
}
# Writes whose destination is the second argument (source first).
_SOURCE_FIRST = frozenset({"copy2", "copyfile", "copytree", "move", "rename"})
# Module-level or integrator receivers: the destination is an argument.
_ARG_RECEIVERS = frozenset({"write_seam", "shutil", "os", "self", "integrator"})
# A resolved destination derived from the gated path stays the gated path.
_PATH_RESOLVERS = frozenset({"ensure_path_within"})
# Gates over a collection of (source, destination, rel) members: the slot they judge.
_GATE_MEMBER_SLOT = {"_keeps_edited_bundle": 1}
# The only writers that may skip the owner: leaves for direct callers or for
# an in-module caller that asked the owner first. Every other writer asks.
_LEAF_WRITERS = frozenset(
    {
        "copy_instruction",
        "copy_instruction_cursor",
        "copy_instruction_windsurf",
        "copy_instruction_claude",
        "copy_prompt",
        "copy_agent",
        "_write_codex_agent",
        "_write_gemini_command",
        "integrate_command",
    }
)
# The exact write condition: force, a missing file, or the bound evidence gate.
_PERMITTED_WRITE = (
    "force or not target_path.is_file() or (gate and gate.authorizes(target_path, rel_path))"
)
# Lockfile hash surfaces that must never authorize an overwrite.
_LOCK_HASH_NAMES = frozenset({"deployed_file_hashes", "lockfile", "_claims", "content_hash"})


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _calls(index, function: ast.AST) -> set[str]:
    return {_call_name(node) for node in index.walk(function) if isinstance(node, ast.Call)}


def _permits_write(node: ast.AST) -> bool:
    """``return False``: the file is not kept, so the caller writes it."""
    return (
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
    )


def _tuple_permits_write(node: ast.AST) -> bool:
    """``return False, False``: the adopt adapter lets the caller write."""
    return (
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Tuple)
        and len(node.value.elts) == 2
        and all(isinstance(elt, ast.Constant) and elt.value is False for elt in node.value.elts)
    )


def _owner_findings(provider: FactsProvider, rule: str) -> list[Violation]:
    _facts, failures = _facts_for(provider, _BASE, rule)
    if failures:
        return list(failures)
    index = provider.tree_index(_BASE)
    owner = index.function(_OWNER) if index is not None else None
    if owner is None:
        return [_summary(rule, _BASE, f"{_OWNER} must own the overwrite decision")]
    permits = [node for node in index.walk(owner) if _permits_write(node)]
    guarded = [
        node
        for node in permits
        if isinstance(parent := index.parent(node), ast.If)
        and node in parent.body
        and ast.unparse(parent.test) == _PERMITTED_WRITE
    ]
    if len(permits) != 1 or guarded != permits:
        return [
            _summary(
                rule,
                _BASE,
                f"{_OWNER} must permit a write only behind force, a missing file or "
                "OverwriteGate.authorizes (one write-permitting return)",
            )
        ]
    findings = []
    for name in _ADAPTERS:
        adapter = index.function(name)
        if (
            adapter is None
            or "keeps_unproven_file" not in _calls(index, adapter)
            or any(_tuple_permits_write(node) for node in index.walk(adapter))
        ):
            findings.append(_summary(rule, _BASE, f"{name} must ask {_OWNER} for every overwrite"))
    return findings


def _is_write(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if name in _EXECUTABLE_WRITES:
        return True
    if name != "open":
        return False
    modes = [*node.args[1:2], *(kw.value for kw in node.keywords if kw.arg == "mode")]
    return any(
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and set(mode.value) & set("wax+")
        for mode in modes
    )


def _gate_test(test: ast.AST) -> bool | None:
    """``True`` for ``<gate>(...)``, ``False`` for ``not <gate>(...)``, else ``None``.

    Only the bare ask decides; any other test (``True or ...``) is not a gate.
    """
    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    call = test.operand if negated else test
    if isinstance(call, ast.Call) and _call_name(call) in _EXECUTABLE_GATES:
        return not negated
    return None


def _leaves(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], (ast.Continue, ast.Return, ast.Raise))


def _destination(call: ast.Call) -> str | None:
    """The path expression a write call replaces."""
    name = _call_name(call)
    func = call.func
    on_path = isinstance(func, ast.Attribute) and not (
        isinstance(func.value, ast.Name) and func.value.id in _ARG_RECEIVERS
    )
    if on_path and name not in _SOURCE_FIRST:
        return ast.unparse(func.value)
    position = 0 if on_path or name not in _SOURCE_FIRST else 1
    if len(call.args) > position:
        return ast.unparse(call.args[position])
    return None


def _gate_call(test: ast.AST) -> ast.Call | None:
    call = test.operand if isinstance(test, ast.UnaryOp) else test
    return call if isinstance(call, ast.Call) else None


def _rebound(index, function: ast.AST, name: str, after: int, before: int) -> bool:
    """*name* is bound again on a line strictly between *after* and *before*."""
    return any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node.id == name
        and after < node.lineno < before
        for node in index.walk(function)
    )


def _same_target(index, function: ast.AST, gate: ast.AST, write: ast.Call) -> bool:
    """The write replaces the gated path, its resolved form, or the gated member slot.

    The binding that reaches the write must be the gated one: neither the
    destination nor the gated path is rebound between the gate and the write.
    """
    call = _gate_call(gate)
    destination = _destination(write)
    if call is None or destination is None or not call.args:
        return False
    gated = ast.unparse(call.args[0])
    first, line = gate.lineno, write.lineno
    if destination == gated:
        return not _rebound(index, function, destination, first, line)
    resolvers = [
        node
        for node in index.walk(function)
        if isinstance(node, ast.Assign)
        and node.lineno < line
        and any(ast.unparse(target) == destination for target in node.targets)
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) in _PATH_RESOLVERS
        and node.value.args
        and ast.unparse(node.value.args[0]) == gated
    ]
    if resolvers:
        nearest = max(resolvers, key=lambda node: node.lineno).lineno
        return not _rebound(index, function, destination, nearest, line) and not _rebound(
            index, function, gated, min(nearest, first), line
        )
    slot = _GATE_MEMBER_SLOT.get(_call_name(call))
    loop = index.parent(write)
    while loop is not None and loop is not function and not isinstance(loop, ast.For):
        loop = index.parent(loop)
    return (
        slot is not None
        and isinstance(loop, ast.For)
        and ast.unparse(loop.iter) == gated
        and isinstance(loop.target, ast.Tuple)
        and len(loop.target.elts) > slot
        and ast.unparse(loop.target.elts[slot]) == destination
        and not _rebound(index, function, destination, loop.lineno, line)
        and not _rebound(index, function, gated, first, line)
    )


def _gated(index, write: ast.Call) -> bool:
    """*write* runs only when the gate for its own destination let it through."""
    child, parent = write, index.parent(write)
    gates: list[ast.AST] = []
    while parent is not None:
        if isinstance(parent, ast.Lambda):
            return False  # a deferred write runs outside the gate's decision
        if isinstance(parent, ast.If) and child in parent.body and _gate_test(parent.test) is False:
            gates.append(parent.test)
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list) and child in block:
                gates.extend(
                    stmt.test
                    for stmt in block[: block.index(child)]
                    if isinstance(stmt, ast.If)
                    and _gate_test(stmt.test) is True
                    and _leaves(stmt.body)
                )
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return any(_same_target(index, parent, gate, write) for gate in gates)
        child, parent = parent, index.parent(parent)
    return False


def _enclosing_function(index, node: ast.AST) -> ast.AST | None:
    parent = index.parent(node)
    while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parent = index.parent(parent)
    return parent


def _executable_findings(provider: FactsProvider, rule: str) -> list[Violation]:
    findings: list[Violation] = []
    for path in _EXECUTABLE_MODULES:
        _facts, failures = _facts_for(provider, path, rule)
        if failures:
            findings.extend(failures)
            continue
        index = provider.tree_index(path)
        if index is None:
            findings.append(_summary(rule, path, "executable writer module must be parseable"))
            continue
        exempt = _UNGATED_EXECUTABLE_WRITES.get(path, {})
        exempt_used: set[tuple[str, str, str | None]] = set()
        for node in index.nodes:
            if not _is_write(node):
                continue
            function = _enclosing_function(index, node)
            qualname = index.function_qualname(function) if function is not None else "<module>"
            write = (_call_name(node), _destination(node))
            if write in exempt.get(qualname, ()):
                exempt_used.add((qualname, *write))
                continue
            if not _gated(index, node):
                findings.append(
                    violation(
                        rule,
                        path,
                        f"{qualname} writes an executable output without depending on "
                        f"{_EDITED_CALL}'s result; edited outputs must be kept",
                        line=node.lineno,
                    )
                )
        listed = {(name, *write) for name, writes in exempt.items() for write in writes}
        findings.extend(
            _summary(rule, path, f"stale ungated-write exemption {entry}: no such write")
            for entry in sorted(listed - exempt_used, key=str)
        )
    path, name = _CANVAS_GATE
    index = provider.tree_index(path)
    gate = index.function(name) if index is not None else None
    if gate is None or _EDITED_CALL not in _calls(index, gate):
        findings.append(_summary(rule, path, f"{name} must ask {_EDITED_CALL} for every member"))
    return findings


def _ledger_findings(provider: FactsProvider, rule: str) -> list[Violation]:
    _facts, failures = _facts_for(provider, _LEDGER, rule)
    if failures:
        return list(failures)
    index = provider.tree_index(_LEDGER)
    if index is None:
        return [_summary(rule, _LEDGER, "deployment ledger must be parseable")]
    findings: list[Violation] = []
    decide = index.function(_AUTHORIZED)
    gate = index.function(_GATE)
    binder = index.function("recorded_writes")
    if decide is None or gate is None or binder is None:
        return [_summary(rule, _LEDGER, f"{_AUTHORIZED}, {_GATE} and recorded_writes must exist")]
    trusted = [
        node
        for node in index.walk(decide)
        if isinstance(node, ast.Call)
        and _call_name(node) == "_trusted_hash"
        and any(ast.unparse(arg) == "self._write_locator" for arg in node.args)
    ]
    # A receipt authorizes only bytes it recorded; the render path reads no receipt.
    bytes_checked = any(
        isinstance(node, ast.If)
        and ast.unparse(node.test) == "trusted is not None and compute_file_hash(path) == trusted"
        for node in index.walk(decide)
    )
    calls = _calls(index, decide)
    if not trusted or not bytes_checked or "render_verdict" not in calls or "verdict" in calls:
        findings.append(
            _summary(
                rule,
                _LEDGER,
                f"{_AUTHORIZED} must decide from a trusted receipt still holding its "
                "recorded bytes, or the receipt-free locked render verdict",
            )
        )
    for node in index.walk(decide):
        name = (
            node.attr
            if isinstance(node, ast.Attribute)
            else node.id
            if isinstance(node, ast.Name)
            else ""
        )
        if name in _LOCK_HASH_NAMES:
            findings.append(
                violation(
                    rule,
                    _LEDGER,
                    f"{_AUTHORIZED} must not read lockfile hashes ({name})",
                    line=node.lineno,
                )
            )
    if "overwrite_authorized" not in _calls(index, gate):
        findings.append(_summary(rule, _LEDGER, f"{_GATE} must ask {_AUTHORIZED}"))
    if not any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "_overwrite_gate"
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and _call_name(node.value) == "OverwriteGate"
        for node in index.walk(binder)
    ):
        findings.append(
            _summary(rule, _LEDGER, "recorded_writes must bind an OverwriteGate to the integrator")
        )
    return findings


def _writer_findings(provider: FactsProvider, rule: str, path: str) -> list[Violation]:
    _facts, failures = _facts_for(provider, path, rule)
    if failures:
        return list(failures)
    index = provider.tree_index(path)
    if index is None:
        return [_summary(rule, path, "integrator must be parseable")]
    calls = {function: _calls(index, function) for function in index.functions()}
    asks = {function.name for function, names in calls.items() if _OWNER_CALL in names}
    findings: list[Violation] = []
    for function, names in calls.items():
        if function.name in asks or not (names & _RAW_WRITES):
            continue
        if function.name not in _LEAF_WRITERS:
            findings.append(
                violation(
                    rule,
                    path,
                    f"{index.function_qualname(function)} writes without {_OWNER_CALL}; "
                    "managed files need deployment evidence",
                    line=function.lineno,
                )
            )
            continue
        callers = [other for other, other_names in calls.items() if function.name in other_names]
        for caller in callers:
            if caller.name not in asks:
                findings.append(
                    violation(
                        rule,
                        path,
                        f"{index.function_qualname(caller)} reaches writer {function.name} "
                        f"without {_OWNER_CALL}; managed files need deployment evidence",
                        line=caller.lineno,
                    )
                )
    return findings


def check_overwrite_evidence(provider: FactsProvider) -> tuple[Violation, ...]:
    """Managed files are replaced only with a receipt or locked-render evidence."""
    rule = GUARD_OVERWRITE_EVIDENCE
    findings = (
        _owner_findings(provider, rule)
        + _ledger_findings(provider, rule)
        + _executable_findings(provider, rule)
    )
    for path in _WRITERS:
        findings.extend(_writer_findings(provider, rule, path))
    return tuple(findings)
