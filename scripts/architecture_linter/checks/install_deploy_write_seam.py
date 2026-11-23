"""Every integrator file write goes through the deploy write seam (#4282).

``InstallTransaction`` restores a failed install by journaling, at
``integration/write_seam.py``, the prior state of each path the first time it
is written. A write that bypasses the seam is never journaled, so a rollback
would leave it on disk. This guard keeps the seam closed-world:

* no module under ``src/apm_cli/integration/`` (other than the seam) calls a
  raw file writer -- ``Path.write_text``/``unlink``/``mkdir``/..., ``shutil``
  and ``os`` copies, moves and removals, ``open`` for writing, or a writer
  helper imported from ``utils`` instead of the seam;
* every seam writer records its path before it writes and reports the
  finished write after it (the journal's post-write seal, #4863);
* only a closed list of functions outside the seam records a path itself,
  and each of them also reports its finished write;
* the transaction listens at the seam;
* the local-bundle deploy (#5046) and its retire deletes (#5130) write
  through the seam too, and its
  command entry runs it under the deploy journal inside its lockfile writer;
* only the commit gate's write plan enters ``planning`` (#4381) and only the
  locked render's private scratch tree enters ``private_scratch``.

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
        "atomic_copy",
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
_AFTER = "record_after_write"
_SEAM_CONTROL = frozenset(
    {"listen", "stop_listening", "planning", "private_scratch", _AFTER, *_RECORDERS}
)
# (path, function) of every caller outside the seam that records a path
# itself (#4863), with the call that reports its finished write.
_JOURNAL_CONFIG = "journal_config_write"
_DIRECT_RECORDERS = {
    ("src/apm_cli/integration/base_integrator.py", "write_text_if_changed"): _AFTER,
    ("src/apm_cli/adapters/client/config_journal.py", "journal_config_write"): None,
    ("src/apm_cli/install/skill_path_migration.py", "execute_migration"): _AFTER,
    ("src/apm_cli/install/skill_path_migration.py", "_cleanup_empty_parents"): _AFTER,
    ("src/apm_cli/adapters/client/base.py", "_write_json_config"): "journal_config_written",
    ("src/apm_cli/adapters/client/codex.py", "_write_config"): "journal_config_written",
    ("src/apm_cli/adapters/client/claude.py", "_merge_user_mcp"): "journal_config_written",
    ("src/apm_cli/adapters/client/hermes.py", "update_config"): "journal_config_written",
    ("src/apm_cli/adapters/client/intellij.py", "_write_config"): "journal_config_written",
}
# Deploy writers outside integration/ that write through the seam (#5046).
_SEAMED_FUNCTIONS = {
    "src/apm_cli/install/services.py": frozenset({"integrate_local_bundle"}),
    # The bundle route's retire deletes (#5130).
    "src/apm_cli/install/local_bundle_hook_trust.py": frozenset(
        {"_retire_per_file", "_remove_unedited"}
    ),
    "src/apm_cli/install/local_bundle_canvas.py": frozenset({"_retire"}),
    "src/apm_cli/install/local_bundle_pending.py": frozenset({"_write"}),
}
_BUNDLE_ENTRY = "src/apm_cli/commands/install.py"
_BUNDLE_JOURNALED = (
    "lockfile_writer(LockfileWriter.INSTALL_BUNDLE)(journaled_local_bundle(_install_lb))("
)
# (path, enclosing function) of the only callers of each journal-suspending mode.
_SEAM_MODES = {
    "planning": frozenset({("src/apm_cli/install/commit_gate.py", "_planned_keeps")}),
    "private_scratch": frozenset({("src/apm_cli/integration/cleanup.py", "_render_locked")}),
}


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


def _bypass_violations(
    provider: FactsProvider, path: str, rule_id: str, scopes: frozenset[str] | None = None
) -> list[Violation]:
    """Raw writes in *path*, or only in its functions named in *scopes*."""
    facts, failures = _facts_for(provider, path, rule_id)
    if failures:
        return list(failures)
    lines = _lines(facts)
    found: list[Violation] = []
    for call in facts.calls:
        if _EXEMPT in lines[call.line - 1] or (scopes is not None and call.scope not in scopes):
            continue
        window = "\n".join(lines[call.line - 1 : call.line + 8])[call.column :]
        writer = _raw_write(call.qualname, _call_text(lines, call.line, call.column), window)
        if writer is None and scopes is not None and call.qualname in _WRITER_HELPERS:
            writer = call.qualname  # a helper imported inside the function
        if writer is not None:
            found.append(
                violation(
                    rule_id,
                    path,
                    f"{writer} bypasses the deploy write seam; use integration/write_seam.py",
                    line=call.line,
                )
            )
    for fact in facts.imports if scopes is None else ():
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
        if (
            not isinstance(node, ast.FunctionDef)
            or node.name in _SEAM_CONTROL
            or node.name.startswith("_")
        ):
            continue
        reported = {
            item.func.id
            for item in ast.walk(node)
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
        }
        if not reported & {_AFTER, "_SealOnClose"}:
            found.append(
                violation(
                    rule_id,
                    _SEAM,
                    f"seam writer {node.name} must report its finished write ({_AFTER})",
                    line=node.lineno,
                )
            )
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


def _mode_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Planning and private scratch suspend the journal only at their closed call sites.

    A mode is matched by its name on any receiver (a module alias included),
    and importing a mode function directly is rejected, so neither can
    escape the closed sites under another spelling.
    """
    found: list[Violation] = []
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _SEAM:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            found += failures
            continue
        found += (
            violation(
                rule_id,
                path,
                f"import write_seam.{name} only as write_seam.{name}()",
                line=fact.line,
            )
            for fact in facts.imports
            for name in sorted(set(fact.names) & set(_SEAM_MODES))
            if (fact.module or "").lstrip(".").endswith("write_seam")
        )
        for call in facts.calls:
            mode = call.qualname.rsplit(".", 1)[-1]
            if mode in _SEAM_MODES and (path, call.scope) not in _SEAM_MODES[mode]:
                found.append(
                    violation(
                        rule_id,
                        path,
                        f"write_seam.{mode} outside its closed call site",
                        line=call.line,
                    )
                )
    return found


def _direct_recorder_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """Recording outside the seam stays on the closed list, and reports each write."""
    found: list[Violation] = []
    recorders = {*_RECORDERS, _JOURNAL_CONFIG}
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _SEAM:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        if failures:
            found += failures
            continue
        names = {call.scope: set() for call in facts.calls}
        for call in facts.calls:
            names[call.scope].add(call.qualname.rsplit(".", 1)[-1])
        for scope, called in names.items():
            if not called & recorders:
                continue
            key = (path, (scope or "").rsplit(".", 1)[-1])
            if key not in _DIRECT_RECORDERS:
                found.append(
                    violation(
                        rule_id,
                        path,
                        f"{key[1]} records a deploy path outside the write seam's closed list",
                    )
                )
            elif _DIRECT_RECORDERS[key] is not None and _DIRECT_RECORDERS[key] not in called:
                found.append(
                    violation(
                        rule_id,
                        path,
                        f"{key[1]} must report its finished write ({_DIRECT_RECORDERS[key]})",
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
    found += _mode_violations(provider, rule_id)
    found += _direct_recorder_violations(provider, rule_id)
    for path, scopes in _SEAMED_FUNCTIONS.items():
        found += _bypass_violations(provider, path, rule_id, scopes)
    entry, failures = _facts_for(provider, _BUNDLE_ENTRY, rule_id)
    found += failures
    if not failures and _BUNDLE_JOURNALED not in "\n".join(_lines(entry)):
        found.append(
            _summary(rule_id, _BUNDLE_ENTRY, "a local-bundle install must run under the journal")
        )
    transaction, failures = _facts_for(provider, _TRANSACTION, rule_id)
    if failures:
        return (*found, *failures)
    transaction_text = "\n".join(_lines(transaction))
    if "write_seam.listen(self._record_deploy_write, self._seal_deploy_write)" not in (
        transaction_text
    ):
        found.append(
            _summary(rule_id, _TRANSACTION, "InstallTransaction must journal at the write seam")
        )
    return tuple(found)


__all__ = ["GUARD_DEPLOY_WRITE_SEAM", "check_deploy_write_seam"]
