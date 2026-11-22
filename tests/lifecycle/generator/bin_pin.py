"""The U-BIN predicate digest the lifecycle generator depends on.

U-BIN (steward ruling) says bin/ deploys only through
``install/exec_gate.py`` ``plugin_bin_deployable``: a marketplace plugin, at
user scope, with a Claude target that supports skills. The closed-world pins
and mutation cases of that domain live with the gate tables
(tests/lifecycle/security, T3). Here the generator only records the digest of
the predicate's source tokens (comments and docstring dropped, so the digest
is stable across Python versions); ``rules.U_BIN_PIN`` holds the approved
value, and a mismatch turns the U-BIN cells back into must-cover obligations.
``user_org_policy_digest`` pins ``core/scope.policy_discovery_uses_git_remote``
the same way for U-USER-SCOPE-ORG-POLICY (``rules.U_USER_ORG_POLICY_PIN``).
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
PREDICATE = ("install/exec_gate.py", "plugin_bin_deployable")
_SKIP = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


def predicate_tokens(src_root: Path = SRC_ROOT) -> str:
    """Source tokens of ``plugin_bin_deployable`` without comments or docstring."""
    return function_tokens(*PREDICATE, src_root=src_root)


def function_tokens(relpath: str, name: str, src_root: Path = SRC_ROOT) -> str:
    """Source tokens of the module-level function *name* without comments or docstring."""
    path = src_root / relpath
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            keep = range(node.lineno - 1, node.end_lineno)
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                keep = [i for i in keep if not first.lineno - 1 <= i < first.end_lineno]
            text = textwrap.dedent("".join(lines[i] for i in keep))
            stream = tokenize.generate_tokens(io.StringIO(text).readline)
            return " ".join(token.string for token in stream if token.type not in _SKIP)
    raise LookupError(f"{PREDICATE[1]} not found in {path}")


@cache
def predicate_digest(src_root: Path = SRC_ROOT) -> str:
    return hashlib.sha256(predicate_tokens(src_root).encode("utf-8")).hexdigest()


# U-USER-SCOPE-ORG-POLICY: the single user-scope org-policy decision (#3703).
USER_ORG_POLICY = ("core/scope.py", "policy_discovery_uses_git_remote")


@cache
def user_org_policy_digest(src_root: Path = SRC_ROOT) -> str:
    tokens = function_tokens(*USER_ORG_POLICY, src_root=src_root)
    return hashlib.sha256(tokens.encode("utf-8")).hexdigest()
