"""Windows held directories: one registration site, released before every APM mutation.

On Windows, path resolution (``utils/realpath_win.py``) holds the project root
and the package directories activation placed open without
``FILE_SHARE_DELETE`` for one CLI invocation. A held directory cannot be
renamed or deleted, so APM must call ``release_held`` before it renames or
deletes a directory that may be held.

Two closed-world rules guard that decision:

* ``transport-platform-held-root-registration``: ``register_held_root`` and
  ``release_held`` are defined only in ``utils/realpath_win.py``, and
  ``register_held_root`` is imported, named and called only by
  ``APMDependencyResolver._activate_validated_package`` in
  ``deps/apm_resolver.py``. Any other caller fails.
* ``transport-platform-held-directory-release``: in the FROZEN inventory
  ``_INVENTORY`` (the modules that rename or delete directories under
  ``apm_modules`` during an invocation: resolution staging and its rollback
  journal, the uninstall refresh and re-anchor journal with its restore,
  ``deps clean``, prune, update, the cache landing, plugin normalization,
  registry extraction and the ``file_ops`` helpers), every raw directory
  mutation -- ``os.rename``/``replace``/``renames``/``rmdir``/``removedirs``,
  ``shutil.rmtree``/``move`` (also through ``import ... as`` and
  ``from os|shutil import``), and ``<path>.rename(x)``/``.replace(x)``
  (exactly one positional argument, no keyword, a receiver that is not a
  string literal: ``str.replace`` always takes two) and ``<path>.rmdir()`` --
  and every link creation -- ``os.symlink`` and ``<path>.symlink_to(x)``, which
  path resolution's per-invocation containment proofs must not outlive --
  must be preceded, in its enclosing function or an enclosing outer function,
  by a ``release_held(...)`` call, or the function must be in ``_ALLOWLIST``
  with a reviewed reason. Routing through ``file_ops.robust_rmtree`` /
  ``robust_replace`` or ``path_security.safe_rmtree`` is not a raw mutation:
  those helpers release first. ``unlink``/``os.remove`` are not counted: they
  remove files or links, and a hold never covers a reparse point.

The inventory is frozen: a NEW module that renames or deletes a directory under
``apm_modules`` is out of scope of static detection and joins ``_INVENTORY`` by
review. An allowlist entry that no longer mutates a directory is stale and fails.

No APM subprocess renames or deletes inside a registered package: git and other
children APM spawns work in the cache, staging or temp trees, which are never
held. User code (lifecycle scripts) runs only after every hold is released.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _load,
    _src_python,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

REGISTRATION_RULE_ID = "transport-platform-held-root-registration"
RELEASE_RULE_ID = "transport-platform-held-directory-release"

_OWNER = "src/apm_cli/utils/realpath_win.py"
_REGISTRAR = "src/apm_cli/deps/apm_resolver.py"
_REGISTRAR_FUNCTION = "APMDependencyResolver._activate_validated_package"
_REGISTER = "register_held_root"
_RELEASE = "release_held"

_INVENTORY = (
    "src/apm_cli/cache/locking.py",
    "src/apm_cli/commands/deps/cli.py",
    "src/apm_cli/commands/prune.py",
    "src/apm_cli/commands/uninstall/engine.py",
    "src/apm_cli/commands/uninstall/local_reanchor.py",
    "src/apm_cli/commands/update.py",
    "src/apm_cli/deps/plugin_parser.py",
    "src/apm_cli/deps/registry/resolver.py",
    "src/apm_cli/install/deploy_snapshot.py",
    "src/apm_cli/install/journal_modes.py",
    "src/apm_cli/install/resolution_staging.py",
    "src/apm_cli/integration/write_seam.py",
    "src/apm_cli/utils/file_ops.py",
)

_STAGING_REASON = "removes a staging directory, a volatile name path resolution never holds"
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("src/apm_cli/cache/locking.py", "_safe_rmtree_staged"): (
        "fallback removal of a cache '.inc.' staging directory, a volatile name never held"
    ),
    (
        "src/apm_cli/install/resolution_staging.py",
        "ResolutionStagingSession._release_staging_lock",
    ): _STAGING_REASON,
    (
        "src/apm_cli/install/resolution_staging.py",
        "ResolutionStagingSession._remove_abandoned_staging_roots",
    ): _STAGING_REASON,
    (
        "src/apm_cli/install/resolution_staging.py",
        "ResolutionStagingSession._remove_staging_root",
    ): _STAGING_REASON,
    ("src/apm_cli/install/resolution_staging.py", "_publish_no_clobber"): (
        "publish_replacement releases the destination first; the source is a staging path"
    ),
    ("src/apm_cli/utils/file_ops.py", "_remove_link"): (
        "removes a symlink or junction itself; a hold never covers a reparse point"
    ),
    ("src/apm_cli/utils/file_ops.py", "robust_copytree._cleanup_partial"): (
        "runs only as _retry_on_lock's before_retry, after its release_held()"
    ),
}

_OS_MUTATIONS = frozenset({"removedirs", "rename", "renames", "replace", "rmdir", "symlink"})
_SHUTIL_MUTATIONS = frozenset({"move", "rmtree"})
_PATH_RENAMES = frozenset({"rename", "replace"})


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _module_aliases(index: TreeIndex) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Names bound to ``os``, to ``shutil``, and to their mutating functions."""
    os_names, shutil_names, bare = {"os"}, {"shutil"}, set()
    for node in index.nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_names.add(alias.asname or "os")
                elif alias.name == "shutil":
                    shutil_names.add(alias.asname or "shutil")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mutating = {"os": _OS_MUTATIONS, "shutil": _SHUTIL_MUTATIONS}.get(node.module or "")
            if mutating is not None:
                bare.update(
                    alias.asname or alias.name for alias in node.names if alias.name in mutating
                )
    return frozenset(os_names), frozenset(shutil_names), frozenset(bare)


def _is_path_mutation(call: ast.Call, func: ast.Attribute) -> bool:
    """``<path>.rename(x)``/``.replace(x)``/``.rmdir()``/``.symlink_to(...)`` on a non-literal."""
    if isinstance(func.value, ast.Constant | ast.JoinedStr):
        return False
    if func.attr == "symlink_to":
        return True
    if call.keywords:
        return False
    if func.attr in _PATH_RENAMES:
        return len(call.args) == 1 and not isinstance(call.args[0], ast.Starred)
    return func.attr == "rmdir" and not call.args


def _is_raw_mutation(
    call: ast.Call, aliases: tuple[frozenset[str], frozenset[str], frozenset[str]]
) -> bool:
    """Whether *call* renames or deletes a directory, or creates a link, without an APM helper."""
    os_names, shutil_names, bare = aliases
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in bare
    if not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id in os_names:
        return func.attr in _OS_MUTATIONS
    if isinstance(receiver, ast.Name) and receiver.id in shutil_names:
        return func.attr in _SHUTIL_MUTATIONS
    return _is_path_mutation(call, func)


def _enclosing_functions(index: TreeIndex, node: ast.AST) -> list[ast.AST]:
    """Enclosing ``def``/``async def`` nodes, nearest first."""
    chain: list[ast.AST] = []
    anchor = index.definition_anchor(node)
    while anchor is not None:
        if isinstance(anchor, FUNCTION_NODES):
            chain.append(anchor)
        anchor = index.definition_anchor(anchor)
    return chain


def _releases_before(index: TreeIndex, function: ast.AST, call: ast.Call) -> bool:
    """Whether *function*'s own body calls ``release_held`` before *call*."""
    position = (call.lineno, call.col_offset)
    return any(
        isinstance(node, ast.Call)
        and _callee(node) == _RELEASE
        and (node.lineno, node.col_offset) < position
        for node in index.own_scope(function)
    )


_RELEASE_MESSAGE = (
    "{name} renames or deletes a directory path resolution may hold on Windows: call "
    "release_held(<path>) before it, route it through file_ops robust_rmtree/robust_replace "
    "or path_security.safe_rmtree, or add a reviewed _ALLOWLIST entry"
)


def _module_release_findings(
    path: str, index: TreeIndex, used: set[tuple[str, str]]
) -> list[Violation]:
    findings: list[Violation] = []
    aliases = _module_aliases(index)
    for node in index.nodes:
        if not isinstance(node, ast.Call) or not _is_raw_mutation(node, aliases):
            continue
        chain = _enclosing_functions(index, node)
        name = index.function_qualname(chain[0]) if chain else "module-level code"
        if (path, name) in _ALLOWLIST:
            used.add((path, name))
            continue
        if any(_releases_before(index, function, node) for function in chain):
            continue
        findings.append(
            violation(
                RELEASE_RULE_ID,
                path,
                _RELEASE_MESSAGE.format(name=name),
                line=node.lineno,
                column=node.col_offset + 1,
            )
        )
    return findings


def _check_release(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every raw directory mutation in the frozen inventory releases held directories first."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    used: set[tuple[str, str]] = set()
    for path in _INVENTORY:
        facts, errors = _load(provider, inventory, RELEASE_RULE_ID, path, parse=True)
        findings.extend(errors)
        index = provider.tree_index(path) if facts is not None else None
        if index is None or index.root is None:
            continue
        findings.extend(_module_release_findings(path, index, used))
    findings.extend(
        violation(
            RELEASE_RULE_ID,
            path,
            f"stale _ALLOWLIST entry: {name} no longer renames or deletes a directory",
        )
        for path, name in sorted(_ALLOWLIST)
        if (path, name) not in used and path in inventory
    )
    return tuple(findings)


def _owner_findings(provider: FactsProvider, inventory: frozenset[str]) -> list[Violation]:
    findings: list[Violation] = []
    facts, errors = _load(provider, inventory, REGISTRATION_RULE_ID, _OWNER, parse=True)
    findings.extend(errors)
    index = provider.tree_index(_OWNER) if facts is not None else None
    if facts is not None and index is not None:
        findings.extend(
            violation(REGISTRATION_RULE_ID, _OWNER, f"{name} must be defined here")
            for name in (_REGISTER, _RELEASE)
            if index.function(name) is None
        )
    facts, errors = _load(provider, inventory, REGISTRATION_RULE_ID, _REGISTRAR, parse=True)
    findings.extend(errors)
    index = provider.tree_index(_REGISTRAR) if facts is not None else None
    if facts is not None and index is not None and index.function(_REGISTRAR_FUNCTION) is None:
        findings.append(
            violation(REGISTRATION_RULE_ID, _REGISTRAR, f"{_REGISTRAR_FUNCTION} must exist")
        )
    return findings


def _registration_findings(path: str, facts: FileFacts) -> list[Violation]:
    registrar_scope = _REGISTRAR_FUNCTION.rsplit(".", 1)[-1]
    sites: list[tuple[int, int, str]] = [
        (d.line, 1, f"define {d.name} only in utils/realpath_win.py")
        for d in facts.definitions
        if d.name in (_REGISTER, _RELEASE) and path != _OWNER
    ]
    if path not in (_OWNER, _REGISTRAR):
        sites.extend(
            (item.line, item.column + 1, f"import {_REGISTER} only in deps/apm_resolver.py")
            for item in facts.imports
            if _REGISTER in item.names
        )
        sites.extend(
            (item.line, item.column + 1, f"name {_REGISTER} only in deps/apm_resolver.py")
            for item in facts.literals
            if item.value_repr == repr(_REGISTER)
        )
    sites.extend(
        (call.line, call.column + 1, f"call {_REGISTER} only from {_REGISTRAR_FUNCTION}")
        for call in facts.calls
        if call.qualname.rsplit(".", 1)[-1] == _REGISTER
        and (path != _REGISTRAR or call.scope != registrar_scope)
    )
    return [
        violation(REGISTRATION_RULE_ID, path, message, line=line, column=column)
        for line, column, message in sites
    ]


def _check_registration(provider: FactsProvider) -> tuple[Violation, ...]:
    """Only activation registers held roots, and only realpath_win defines the hold API."""
    inventory = frozenset(provider.inventory)
    findings = _owner_findings(provider, inventory)
    for path in _src_python(provider):
        lines, _error = provider.lexical_lines(path)
        if not any(_REGISTER in line or _RELEASE in line for line in lines):
            continue
        facts, errors = _load(provider, inventory, REGISTRATION_RULE_ID, path, parse=True)
        findings.extend(errors)
        if facts is not None:
            findings.extend(_registration_findings(path, facts))
    return tuple(findings)


RULES = (
    Rule(
        id=REGISTRATION_RULE_ID,
        group=GROUP,
        guard_ids=(REGISTRATION_RULE_ID,),
        description="Only package activation registers Windows held roots.",
        check=_check_registration,
    ),
    Rule(
        id=RELEASE_RULE_ID,
        group=GROUP,
        guard_ids=(RELEASE_RULE_ID,),
        description="APM releases Windows held directories before it renames or deletes them.",
        check=_check_release,
    ),
)
