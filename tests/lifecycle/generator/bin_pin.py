"""The U-BIN source digest the lifecycle generator depends on (#4894).

U-BIN (steward ruling, re-derived in #4894) names where bin/ can reach project
scope. Loose bin/ deploys only through ``install/exec_gate.py``
``plugin_bin_deployable`` (a marketplace plugin, at user scope, with a Claude
target that supports skills). A trusted Claude plugin that ships bin/ deploys
whole at project scope: ``claude_plugins/admission.py`` keeps bin/ out of
``_UNSUPPORTED_ROOTS`` (``--trust-bin`` and the whole-plugin approval decide
it), and the root lands under ``native_plugins/record.py``
``PLUGINS_RELATIVE``. The closed-world pins and mutation cases of the gate
tables live with tests/lifecycle/security (T3). Here the generator only
records the digest of those elements' source tokens (comments and docstrings
dropped, so the digest is stable across Python versions); ``rules.U_BIN_PIN``
holds the approved value, and a mismatch turns the project-scope bin cells
into must-cover obligations.
"""

from __future__ import annotations

import ast
import hashlib
import io
import textwrap
import tokenize
from functools import cache
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "apm_cli"
# (module, top-level function or assignment) of every pinned element.
PREDICATES = (
    ("install/exec_gate.py", "plugin_bin_deployable"),
    ("claude_plugins/admission.py", "_UNSUPPORTED_ROOTS"),
    ("native_plugins/record.py", "PLUGINS_RELATIVE"),
)
_SKIP = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


def _named(node: ast.stmt, name: str) -> bool:
    if isinstance(node, ast.FunctionDef):
        return node.name == name
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == name for target in node.targets
    )


def element_tokens(path: Path, name: str) -> str:
    """Source tokens of top-level *name* in *path*, without comments or docstring."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        if not _named(node, name):
            continue
        keep = list(range(node.lineno - 1, node.end_lineno))
        first = node.body[0] if isinstance(node, ast.FunctionDef) else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            keep = [i for i in keep if not first.lineno - 1 <= i < first.end_lineno]
        text = textwrap.dedent("".join(lines[i] for i in keep))
        stream = tokenize.generate_tokens(io.StringIO(text).readline)
        return " ".join(token.string for token in stream if token.type not in _SKIP)
    raise LookupError(f"{name} not found in {path}")


def predicate_tokens(src_root: Path = SRC_ROOT) -> str:
    """Tokens of every pinned element, each prefixed with its module and name."""
    return "\n".join(
        f"{module}::{name} {element_tokens(src_root / module, name)}" for module, name in PREDICATES
    )


@cache
def predicate_digest(src_root: Path = SRC_ROOT) -> str:
    return hashlib.sha256(predicate_tokens(src_root).encode("utf-8")).hexdigest()
