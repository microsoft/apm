#!/usr/bin/env python3
"""Guard HookIntegrator as the sole writer of merge-hook JSON/sidecar state.

``src/apm_cli/integration/hook_integrator.py`` is the sole canonical owner
of merge-hook config mutation: ``.claude/settings.json``,
``.codex/hooks.json``, ``.cursor/...``, etc., and their ``apm-hooks.json``
ownership sidecars. A competing owner writing or deleting one of these
files directly -- bypassing schema-strict handling, sidecar bookkeeping,
and path containment -- would silently reintroduce the class of bug this
checker exists to prevent (#2250/#2253).

A private-symbol-containment check (grepping for ``_MERGE_HOOK_TARGETS``
or ``_APM_HOOKS_SIDECAR`` outside this file) only prevents *reuse of those
exact names*. A competing owner could still write or unlink
``.codex/hooks.json`` or an ``apm-hooks.json`` sidecar through a literal
or composed path expression that never references either private symbol.
This checker closes that gap with a semantic AST check rather than a
lexical one.

Scope: every ``*.py`` file under ``src/apm_cli/`` except
``src/apm_cli/integration/hook_integrator.py`` itself. Test fixtures are
out of scope (under ``tests/``) -- they legitimately hand-author
``.codex/hooks.json`` files for setup.

Detected call shapes (any of):
  * ``open(<path>, <mode>)`` / ``open(<path>, mode=<mode>)``
  * ``<path>.open(<mode>)`` / ``<path>.open(mode=<mode>)``
  * ``<path>.write_text(...)`` / ``<path>.write_bytes(...)``
  * ``<path>.unlink(...)``

For the two ``open``-shaped forms, a mode string is only treated as
mutating if it contains ``w``, ``a``, ``x``, or ``+`` (an omitted mode --
``open()``'s own default -- or one that is only ``r`` is read-only and is
skipped). ``write_text``/``write_bytes``/``unlink`` are always mutating
and carry no mode argument to check.

Path resolution uses bounded, one-hop intraprocedural alias resolution
(mirroring the pattern in ``scripts/check_skill_subset_owner.py``): within
each function's own scope (module-level top-level statements count as one
implicit scope too), every simple ``name = <path-composition-expr>``
assignment is recorded against ``name``'s string-constant fragments first.
Then, for each write-mutating call found, its ``<path>`` operand is
resolved as: if the operand is a bare ``Name`` recorded in this same
scope's assignment map, use those recorded fragments; otherwise walk the
operand's own subtree directly for string constants. This closes the
natural bypass shape of assigning a composed path to a local variable
before mutating it, e.g.::

    hook_path = Path(root) / ".codex" / "hooks.json"
    hook_path.write_text("{}")   # or: hook_path.open("w")

A violation is flagged when the resolved fragment set contains either:
  * a fragment containing ``"apm-hooks.json"`` (the sidecar filename is
    distinctive enough alone to need no root-dir pairing), or
  * a fragment exactly matching a known merge-hook root dir (``.claude``,
    ``.codex``, ``.cursor``, ``.gemini``, ``.windsurf``, ``.antigravity``)
    *together with* a fragment exactly matching a known merge-hook config
    filename (``hooks.json``, ``settings.json``) -- requiring both avoids
    flagging the many unrelated ``settings.json`` files elsewhere in the
    codebase/ecosystem that never pair with one of these root-dir
    literals.

The root-dir/filename lists below are static-analysis enforcement data
local to this checker -- they are read-only lint data, wired one
direction only (into this script), and never imported by or exported to
runtime code. Production dropped-target resolution
(``HookIntegrator.reconcile_dropped_targets``) still receives its target
name list from ``manifest_reconcile.py`` and resolves each through the
real ``KNOWN_TARGETS``/``_MERGE_HOOK_TARGETS`` lookups -- this list does
not shadow or duplicate that catalog.

This is intentionally NOT a general dataflow analyzer: alias resolution
is bounded to one hop, same-scope only, and only ``Name = <expr>``
assignments with a single target are tracked.

A configured root that is missing or not a directory is a
misconfiguration, not "nothing to check": ``find_violations()`` fails
closed by raising ``ConfiguredPathError``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

OWNER_MODULE = "src/apm_cli/integration/hook_integrator.py"
SCAN_ROOT = "src/apm_cli"

_MERGE_HOOK_ROOT_DIRS = frozenset(
    {".claude", ".codex", ".cursor", ".gemini", ".windsurf", ".antigravity"}
)
_MERGE_HOOK_FILENAMES = frozenset({"hooks.json", "settings.json"})
_SIDECAR_FILENAME_FRAGMENT = "apm-hooks.json"

_MUTATING_OPEN_MODE_CHARS = frozenset("waX+")  # deliberately excludes bare "r"
_WRITE_METHOD_NAMES = frozenset({"write_text", "write_bytes", "unlink"})


@dataclass(frozen=True)
class Violation:
    """A single write-mutating call to a merge-hook config path outside HookIntegrator."""

    path: Path
    line: int
    qualname: str

    def render(self) -> str:
        """Return a one-line, actionable diagnostic for this violation."""
        return (
            f"{self.path}:{self.line}: '{self.qualname}' writes/deletes a merge-hook "
            f"config or sidecar path directly -- this must stay owned by "
            f"HookIntegrator ({OWNER_MODULE}), not reimplemented here."
        )


class ConfiguredPathError(RuntimeError):
    """Raised when the configured scan root is missing or not a directory.

    ``--root`` is a hard-coded repository root wired from
    scripts/lint-architecture-boundaries.sh, not user input: if the
    computed scan directory does not exist, the checker cannot scan it and
    must fail closed rather than silently reporting "no violations",
    which would let a misconfigured root evade the guard entirely.
    """


def _mode_is_mutating(mode: ast.AST | None) -> bool:
    """Return True if a mode argument (or its absence) implies a mutating open.

    An omitted mode defaults to Python's own ``"r"`` and is NOT mutating.
    A non-constant (dynamically computed) mode is conservatively treated
    as mutating, since we cannot prove it is read-only.
    """
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(ch in _MUTATING_OPEN_MODE_CHARS for ch in mode.value)
    return True


def _open_call_mode_arg(node: ast.Call, *, mode_positional_index: int) -> ast.AST | None:
    """Return the mode argument of a Call, whether positional or keyword.

    ``mode_positional_index`` is 1 for the builtin ``open(path, mode)``
    shape (mode is the second positional argument) and 0 for the method
    ``path.open(mode)`` shape (mode is the first, since ``path`` is the
    call's receiver, not a positional argument).
    """
    if len(node.args) > mode_positional_index:
        return node.args[mode_positional_index]
    for kw in node.keywords:
        if kw.arg == "mode":
            return kw.value
    return None


def _mutating_path_operand(node: ast.AST) -> ast.AST | None:
    """Return the path-like operand of a write-mutating call, or None.

    Recognizes: builtin ``open(path, mode)``; ``path.open(mode)``;
    ``path.write_text(...)``; ``path.write_bytes(...)``; ``path.unlink(...)``.
    """
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


def _string_constants(node: ast.AST) -> set[str]:
    """Collect every string-constant literal fragment in node's subtree."""
    fragments: set[str] = set()
    for descendant in ast.walk(node):
        if isinstance(descendant, ast.Constant) and isinstance(descendant.value, str):
            fragments.add(descendant.value)
    return fragments


def _path_alias_fragments(scope_nodes: Sequence[ast.AST]) -> dict[str, set[str]]:
    """Map simple ``name = <path-composition-expr>`` locals to their string fragments.

    Only single-target ``Name = <expr>`` assignments are tracked -- this is
    a bounded, one-hop alias resolution, not general dataflow analysis; it
    does not follow reassignment, tuple unpacking, or attribute/subscript
    targets.
    """
    aliases: dict[str, set[str]] = {}
    for node in scope_nodes:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            aliases[target.id] = _string_constants(node.value)
    return aliases


def _resolve_operand_fragments(operand: ast.AST, aliases: dict[str, set[str]]) -> set[str]:
    """Resolve a call's path operand to its string-constant fragments.

    If the operand is a bare ``Name`` recorded in this scope's alias map,
    use its recorded fragments (the one-hop alias-resolution case).
    Otherwise walk the operand's own subtree directly.
    """
    if isinstance(operand, ast.Name) and operand.id in aliases:
        return aliases[operand.id]
    return _string_constants(operand)


def _is_hook_config_path(fragments: set[str]) -> bool:
    """Return True if fragments identify a merge-hook config or sidecar path."""
    if any(_SIDECAR_FILENAME_FRAGMENT in fragment for fragment in fragments):
        return True
    has_root_dir = any(fragment in _MERGE_HOOK_ROOT_DIRS for fragment in fragments)
    has_filename = any(fragment in _MERGE_HOOK_FILENAMES for fragment in fragments)
    return has_root_dir and has_filename


def _walk_own_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of node without descending into nested scopes.

    Nested ``def``/``async def``/``lambda`` bodies are excluded so each
    function (and the module's own top-level statements) is judged solely
    on its own statements.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield from _walk_own_scope(child)


def _build_qualnames(tree: ast.Module) -> dict[ast.AST, str]:
    """Map each function/method node (and the module itself) to a dotted qualified name."""
    qualnames: dict[ast.AST, str] = {tree: "<module>"}

    def walk(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, [*prefix, child.name])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = [*prefix, child.name]
                qualnames[child] = ".".join(name)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, [])
    return qualnames


def _python_files(root: Path) -> list[Path]:
    """Return every ``*.py`` file under ``root`` except the owner module itself."""
    scan_dir = root / SCAN_ROOT
    if not scan_dir.is_dir():
        raise ConfiguredPathError(f"{scan_dir}: configured scan root is not a directory")
    owner_path = (root / OWNER_MODULE).resolve()
    files = [
        path for path in scan_dir.rglob("*.py") if path.is_file() and path.resolve() != owner_path
    ]
    return sorted(files)


def find_violations(root: Path) -> list[Violation]:
    """Return every merge-hook config write/delete found outside HookIntegrator.

    ``root`` must contain ``src/apm_cli`` as a directory; a missing scan
    root raises ``ConfiguredPathError`` instead of being silently treated
    as "no violations" (see ``ConfiguredPathError``). Files that exist but
    fail to parse as Python are skipped, since a syntax error is a
    pre-existing problem unrelated to this narrow, targeted check.
    """
    violations: list[Violation] = []
    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue

        qualnames = _build_qualnames(tree)
        for scope_node, qualname in qualnames.items():
            scope_nodes = (
                list(_walk_own_scope(scope_node))
                if scope_node is not tree
                else [n for n in ast.iter_child_nodes(tree)]
            )
            aliases = _path_alias_fragments(scope_nodes)
            for node in scope_nodes:
                operand = _mutating_path_operand(node)
                if operand is None:
                    continue
                fragments = _resolve_operand_fragments(operand, aliases)
                if _is_hook_config_path(fragments):
                    violations.append(
                        Violation(
                            path=path.relative_to(root) if path.is_relative_to(root) else path,
                            line=node.lineno,
                            qualname=qualname,
                        )
                    )

    violations.sort(key=lambda v: (str(v.path), v.line))
    return violations


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments for the checker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing src/apm_cli (default: repo root inferred from script location).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker over the configured root and return a process exit code.

    Fails closed (nonzero, with an actionable diagnostic) if the scan root
    is misconfigured, rather than reporting a misleading "no violations
    found" for a root that was never scanned.
    """
    args = _parse_args(argv)
    try:
        violations = find_violations(args.root.resolve())
    except ConfiguredPathError as exc:
        print(f"[x] {exc}")
        return 1
    if violations:
        for violation in violations:
            print(f"[x] {violation.render()}")
        print(f"[x] {len(violations)} hook config write-owner violation(s) found")
        return 1
    print("[+] no hook config write-owner violations found outside HookIntegrator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
