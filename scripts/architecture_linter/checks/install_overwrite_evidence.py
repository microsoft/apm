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
* each executable writer (Copilot and Kiro hook files, the hook script copy,
  the canvas bundle) asks ``keeps_edited_output`` before it writes.
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
# Executable writers: each named function asks keeps_edited_output (#4626).
_EXECUTABLE_WRITERS = {
    "src/apm_cli/integration/hook_integrator.py": ("HookIntegrator.integrate_package_hooks",),
    "src/apm_cli/integration/hook_bundle.py": ("copy_deployed_hook_bundle",),
    "src/apm_cli/integration/kiro_hook_integrator.py": ("_write_kiro_hook_docs",),
    "src/apm_cli/integration/canvas_integrator.py": ("CanvasIntegrator._keeps_edited_bundle",),
}
_CANVAS_DEPLOY = ("src/apm_cli/integration/canvas_integrator.py", "CanvasIntegrator._deploy_bundle")
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
# One keeps_edited_output ask per write in each executable writer.
_EXECUTABLE_WRITES = _RAW_WRITES | {"atomic_write_text"}
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


def _executable_findings(provider: FactsProvider, rule: str) -> list[Violation]:
    findings: list[Violation] = []
    for path, names in _EXECUTABLE_WRITERS.items():
        _facts, failures = _facts_for(provider, path, rule)
        if failures:
            findings.extend(failures)
            continue
        index = provider.tree_index(path)
        for name in names:
            function = index.function(name) if index is not None else None
            names_called = (
                [_call_name(node) for node in index.walk(function) if isinstance(node, ast.Call)]
                if function is not None
                else []
            )
            writes = sum(called in _EXECUTABLE_WRITES for called in names_called)
            if names_called.count(_EDITED_CALL) < max(1, writes):
                findings.append(
                    _summary(
                        rule,
                        path,
                        f"{name} must ask {_EDITED_CALL} before replacing an executable output",
                    )
                )
    path, name = _CANVAS_DEPLOY
    index = provider.tree_index(path)
    deploy = index.function(name) if index is not None else None
    if deploy is None or "_keeps_edited_bundle" not in _calls(index, deploy):
        findings.append(
            _summary(rule, path, f"{name} must leave a bundle with a kept member whole")
        )
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
