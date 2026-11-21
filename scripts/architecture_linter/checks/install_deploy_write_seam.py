"""Every integrator file write goes through the deploy write seam (#4282).

``InstallTransaction`` restores a failed install by journaling, at
``integration/write_seam.py``, the prior state of each path the first time it
is written. A write that bypasses the seam is never journaled, so a rollback
would leave it on disk. This guard keeps the seam closed-world:

* no module under ``src/apm_cli/integration/`` (other than the seam) calls a
  raw file writer -- ``Path.write_text``/``unlink``/``mkdir``/..., ``shutil``
  and ``os`` copies, moves and removals, ``open`` for writing, or a writer
  helper imported from ``utils`` instead of the seam;
* every seam writer records its path before it writes;
* the transaction listens at the seam.

A line marked ``# write-seam-exempt: <reason>`` (a private scratch tree) is
skipped.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _lines,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_DEPLOY_WRITE_SEAM = "install-deployment-deploy-write-seam"

_INTEGRATION_PREFIX = "src/apm_cli/integration/"
_SEAM = "src/apm_cli/integration/write_seam.py"
_TRANSACTION = "src/apm_cli/install/transaction.py"
_EXEMPT = "# write-seam-exempt:"
_SEAM_RECEIVER = "write_seam"

# Path methods that create, replace or remove a file or directory.
_RAW_METHODS = frozenset(
    {"hardlink_to", "mkdir", "rename", "rmdir", "symlink_to", "touch", "unlink", "write_bytes"}
    | {"write_text"}
)
_RAW_MODULE_CALLS = frozenset(
    {
        "os.link",
        "os.makedirs",
        "os.mkdir",
        "os.remove",
        "os.removedirs",
        "os.rename",
        "os.renames",
        "os.replace",
        "os.rmdir",
        "os.symlink",
        "os.truncate",
        "os.unlink",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
    }
)
# Writer helpers that must come from the seam, never from their utils module.
_WRITER_HELPERS = frozenset(
    {
        "atomic_write_bytes",
        "atomic_write_text",
        "robust_rmtree",
        "safe_rmtree",
        "write_json_config",
        "write_text_lf",
    }
)
_SEAM_MODULES = frozenset({"apm_cli.integration.write_seam", "write_seam"})
_WRITE_MODE = re.compile(r"""(?:,\s*|mode\s*=\s*)["'][rbt]*[wax+][rbt+]*["']""")
_WRITE_MODE_TEXT = re.compile(r"^[rbt]*[wax+][rbt+]*$")
_WRITE_FLAGS = re.compile(r"\bos\.O_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)\b")
_RECORDERS = frozenset({"record_before_write", "record_before_mkdir"})
_SEAM_CONTROL = frozenset({"listen", "stop_listening", *_RECORDERS})


def _call_text(lines: tuple[str, ...], line: int, column: int) -> str:
    """The call starting at (*line*, *column*) through its balanced parenthesis."""
    text = "\n".join(lines[line - 1 : line + 8])[column:]
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[: index + 1]
    return text


def _raw_write(qualname: str, call_text: str, window: str = "") -> str | None:
    """Name the raw writer *qualname* calls, or ``None`` when it is not one."""
    receiver, _dot, method = qualname.rpartition(".")
    if receiver == _SEAM_RECEIVER:
        return None
    if qualname in _RAW_MODULE_CALLS:
        return qualname
    if receiver and method in _RAW_METHODS:
        return f".{method}()"
    if receiver and method in _WRITER_HELPERS:
        return qualname
    if qualname == "open" and _WRITE_MODE.search(call_text):
        return "open() for writing"
    if qualname == "os.open":
        return "os.open() for writing" if _WRITE_FLAGS.search(call_text) else None
    return _path_method_write(method, window) if receiver else None


def _path_method_write(method: str, window: str) -> str | None:
    """``Path.open`` in a writing mode, or ``Path.replace``."""
    if method == "open" and _opens_for_writing(_method_call(window, "open")):
        return ".open() for writing"
    replace = _method_call(window, "replace") if method == "replace" else None
    # Path.replace(target) takes one argument; str/bytes.replace take two.
    one_arg = replace is not None and len(replace.args) == 1 and not replace.keywords
    return ".replace()" if one_arg else None


def _method_call(window: str, method: str) -> ast.Call | None:
    """Parse the argument list of the first ``.method(...)`` call in *window*.

    *window* starts at the call's receiver, so a chained call
    (``a.group(1).replace(x, y)``) is judged by the method's own arguments.
    """
    start = window.find(f".{method}(")
    if start < 0:
        return None
    start += len(method) + 1
    depth = 0
    for index in range(start, len(window)):
        depth += {"(": 1, ")": -1}.get(window[index], 0)
        if depth == 0:
            try:
                call = ast.parse("f" + window[start : index + 1], mode="eval").body
            except SyntaxError:
                return None
            return call if isinstance(call, ast.Call) else None
    return None


def _opens_for_writing(call: ast.Call | None) -> bool:
    """Whether a ``Path.open`` call names a writing mode (positional or ``mode=``)."""
    if call is None:
        return False
    modes = [*call.args[:1], *(k.value for k in call.keywords if k.arg == "mode")]
    return any(
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and _WRITE_MODE_TEXT.match(mode.value) is not None
        for mode in modes
    )


def _bypass_violations(provider: FactsProvider, path: str, rule_id: str) -> list[Violation]:
    facts, failures = _facts_for(provider, path, rule_id)
    if failures:
        return list(failures)
    lines = _lines(facts)
    found: list[Violation] = []
    for call in facts.calls:
        if _EXEMPT in lines[call.line - 1]:
            continue
        window = "\n".join(lines[call.line - 1 : call.line + 8])[call.column :]
        writer = _raw_write(call.qualname, _call_text(lines, call.line, call.column), window)
        if writer is not None:
            found.append(
                violation(
                    rule_id,
                    path,
                    f"{writer} bypasses the deploy write seam; use integration/write_seam.py",
                    line=call.line,
                )
            )
    for fact in facts.imports:
        module = (fact.module or "").lstrip(".")
        helpers = sorted(set(fact.names) & _WRITER_HELPERS)
        if helpers and module not in _SEAM_MODULES and _EXEMPT not in lines[fact.line - 1]:
            found.append(
                violation(
                    rule_id,
                    path,
                    f"import {', '.join(helpers)} from the deploy write seam, not {module}",
                    line=fact.line,
                )
            )
    return found


def _seam_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Every seam writer records its path before its first other call."""
    text, _error = provider.source_cache.read(_SEAM)
    if not text:
        return [_summary(rule_id, _SEAM, "the deploy write seam is missing")]
    found: list[Violation] = []
    for node in ast.parse(text).body:
        if not isinstance(node, ast.FunctionDef) or node.name in _SEAM_CONTROL:
            continue
        calls = [
            item.value.func
            for item in node.body
            if isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
        ]
        first = calls[0] if calls else None
        if not (isinstance(first, ast.Name) and first.id in _RECORDERS):
            found.append(
                violation(
                    rule_id,
                    _SEAM,
                    f"seam writer {node.name} must record its path before it writes",
                    line=node.lineno,
                )
            )
    return found


def check_deploy_write_seam(provider: FactsProvider) -> tuple[Violation, ...]:
    """Integrator file writes go through the seam the install transaction journals."""
    rule_id = GUARD_DEPLOY_WRITE_SEAM
    found: list[Violation] = []
    for path in _python_paths(provider, _INTEGRATION_PREFIX):
        if path != _SEAM:
            found += _bypass_violations(provider, path, rule_id)
    found += _seam_violations(provider, rule_id)
    transaction, failures = _facts_for(provider, _TRANSACTION, rule_id)
    if failures:
        return (*found, *failures)
    transaction_text = "\n".join(_lines(transaction))
    if "write_seam.listen(self._record_deploy_write)" not in transaction_text:
        found.append(
            _summary(rule_id, _TRANSACTION, "InstallTransaction must journal at the write seam")
        )
    return tuple(found)


__all__ = ["GUARD_DEPLOY_WRITE_SEAM", "check_deploy_write_seam"]
