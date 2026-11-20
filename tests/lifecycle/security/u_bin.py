"""Closed-world pin of the U-BIN definition rule (bin/ deploy domain).

U-BIN removes gate cells for bin/ on paths where bin/ can never deploy. It
holds only while three source elements keep their approved shape:

- ``plugin_bin_deployable`` (install/exec_gate.py): a marketplace plugin, at
  user scope, with a Claude target that supports skills;
- the bin/ writer: ``SkillIntegrator._deploy_bin_files`` is called only from
  ``_deploy_plugin_bin``, which has a single caller;
- ``apm unpack`` refuses every executable file (bundle/unpacker.py).

``pin_elements`` reads each element from source (tokens without comments or
docstrings, so the pin is stable across Python versions and formatting) and
``pin_digest`` hashes them. ``U_BIN_PIN`` records the approved digest. While
the live digest equals it, the gate removal ids in ``U_BIN_CELLS`` (except
each shape's witness row) are excluded from the G space
(``tests/quality/security_obligations.json`` ``excluded``). When it differs,
``pin_holds`` is False, the exclusions lapse and every U-BIN id is a required
obligation again until a reviewed change updates the pin.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import io
import json
import textwrap
import tokenize
from collections.abc import Mapping
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
_EXEC_GATE = "install/exec_gate.py"
_INTEGRATOR = "integration/skill_integrator.py"
_UNPACKER = "bundle/unpacker.py"
# Calls whose callers are the closed set of bin/ writers.
_WRITER_CALLS = ("_deploy_bin_files", "_deploy_plugin_bin")
_SKIP = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


def _tokens(source: str) -> str:
    stream = tokenize.generate_tokens(io.StringIO(source).readline)
    return " ".join(token.string for token in stream if token.type not in _SKIP)


def _definition(path: Path, name: str) -> str:
    """Tokens of top-level function or assignment *name*, without a docstring."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        named = isinstance(node, ast.FunctionDef) and node.name == name
        named = named or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        )
        if not named:
            continue
        keep = list(range(node.lineno - 1, node.end_lineno))
        first = node.body[0] if isinstance(node, ast.FunctionDef) else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            keep = [i for i in keep if not first.lineno - 1 <= i < first.end_lineno]
        return _tokens(textwrap.dedent("".join(lines[i] for i in keep)))
    raise LookupError(f"{name} not found in {path}")


def _callers(src_root: Path) -> list[str]:
    """``module:qualified.function -> call`` for every call of a bin/ writer in *src_root*."""
    found: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        module = path.relative_to(src_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[tuple[ast.AST, str]] = [(tree, "")]
        while stack:
            node, scope = stack.pop()
            for child in ast.iter_child_nodes(node):
                name = scope
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    name = f"{scope}.{child.name}" if scope else child.name
                if isinstance(child, ast.Call):
                    func = child.func
                    called = func.attr if isinstance(func, ast.Attribute) else None
                    called = func.id if isinstance(func, ast.Name) else called
                    if called in _WRITER_CALLS:
                        found.append(f"{module}:{scope or '<module>'} -> {called}")
                stack.append((child, name))
    return sorted(found)


def _unpack_refuses(src_root: Path) -> bool:
    """``unpack_bundle`` calls ``_refuse_executable_content`` on the bundle's files."""
    tree = ast.parse((src_root / _UNPACKER).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "unpack_bundle":
            return any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_refuse_executable_content"
                for call in ast.walk(node)
            )
    return False


def pin_elements(src_root: Path = SRC_ROOT) -> dict[str, object]:
    """The pinned source elements of U-BIN, by name."""
    unpacker = src_root / _UNPACKER
    return {
        "plugin_bin_deployable": _definition(src_root / _EXEC_GATE, "plugin_bin_deployable"),
        "bin_writers": _callers(src_root),
        "unpack_refuses_executables": _unpack_refuses(src_root),
        "unpack_executable_dirs": _definition(unpacker, "_EXECUTABLE_DIR_NAMES"),
        "unpack_executable_kind": _definition(unpacker, "executable_content_kind"),
        "unpack_refusal": _definition(unpacker, "_refuse_executable_content"),
    }


def pin_digest(src_root: Path = SRC_ROOT) -> str:
    canonical = json.dumps(pin_elements(src_root), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# The approved digest of pin_elements() (steward ruling on U-BIN).
U_BIN_PIN = "632062aa61b6d33035fea09d8f5c56cfead40809a19fa62d75cdf6748ff4c6b4"
_GATE_CHANGES = ("local-deny", "org-deny", "org-enables-gate", "policy-dep-deny", "revoke-approval")
_LOCAL_CHANGES = ("local-deny", "revoke-approval")
# Gate removal paths where bin/ never deploys, and the gate changes each one
# replays. Only marketplace-install (and native-plugin) deploy bin/.
U_BIN_PATHS: Mapping[str, tuple[str, ...]] = {
    "install": _GATE_CHANGES,
    "frozen": _GATE_CHANGES,
    "update": _GATE_CHANGES,
    "uninstall-prune-rebuild": _GATE_CHANGES,
    "audit-ci": _GATE_CHANGES,
    "dry-run": ("local-deny", "org-deny", "revoke-approval"),
    "install-g": _LOCAL_CHANGES,
    "frozen-g": _LOCAL_CHANGES,
    "bundle-install": ("local-deny", "org-deny", "org-enables-gate", "revoke-approval"),
    "unpack": _GATE_CHANGES,
}
# Path -> the path shape whose absence row (gate_slices/bin.py) witnesses it.
U_BIN_SHAPES: Mapping[str, str] = {
    "install": "install",
    "frozen": "install",
    "update": "install",
    "uninstall-prune-rebuild": "install",
    "audit-ci": "install",
    "install-g": "install-g",
    "frozen-g": "install-g",
    "dry-run": "dry-run",
    "bundle-install": "bundle-install",
    "unpack": "unpack",
}
U_BIN_CELLS = tuple(
    sorted(f"G|{path}|{change}|bin" for path, changes in U_BIN_PATHS.items() for change in changes)
)


def u_bin_witness(cell: str) -> str:
    """The shape witness row of U-BIN cell ``G|<path>|<change>|bin``."""
    return f"G|{U_BIN_SHAPES[cell.split('|')[1]]}|local-deny|bin"


@functools.cache
def _live_digest() -> str:
    return pin_digest()


def pin_holds(src_root: Path | None = None) -> bool:
    """True while the pinned source elements keep their approved shape."""
    digest = _live_digest() if src_root is None else pin_digest(src_root)
    return digest == U_BIN_PIN
