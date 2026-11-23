"""Every Git run in a repository APM persists goes through the cached-repo owner.

``cache/cached_repo.py`` owns the trust decision for persisted repositories
(cache bares and checkouts, native snapshots): their config is allow-listed
before Git runs there, and every run is inert. This guard keeps that closed:

* in each guarded file, every Git runner's argv carries the inert args (the
  literal list, or a list name assigned in the same function);
* in the cache and snapshot files, its ``env`` comes from ``cached_repo_env``
  (directly, or a name only ever assigned from it, or a parameter);
* only ``git_cache.py`` resolves the git cache buckets, so no other module
  can run Git in them;
* the trust checks stay at every point a persisted repository is opened.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.lexical_shared import load_required, source_text
from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _forbid_scan,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-cached-repo-git"
_OWNER = "src/apm_cli/cache/cached_repo.py"
_GIT_CACHE = "src/apm_cli/cache/git_cache.py"
_SNAPSHOT = "src/apm_cli/codex_plugins/snapshot.py"
_LEGACY = "src/apm_cli/core/legacy_lock_evidence.py"
# file -> (inert-args helper, env must be fenced)
_SITES: dict[str, tuple[str, bool]] = {
    _GIT_CACHE: ("_safe_git_args", True),
    _SNAPSHOT: ("inert_git_args", True),
    _LEGACY: ("inert_git_args", False),
    _OWNER: ("inert_git_args", True),
    "src/apm_cli/cache/checkout_verify.py": ("inert_git_args", True),
}
_RUNNERS = frozenset(
    {
        "run_git_text",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
_ARG_RUNNERS = frozenset(
    {"apply_sparse_cone", "materialize_tracked_symlinks", "validate_materialized_symlinks"}
)
_ENV_FENCE = "cached_repo_env"
_REQUIRED: dict[str, tuple[str, ...]] = {
    _OWNER: (
        'raise UntrustedCachedRepoError(f"config key {shown} is not one APM writes")',
        'raise UntrustedCachedRepoError("object alternates leave the cache")',
        'raise UntrustedCachedRepoError("active hook present")',
        '"-c", "core.fsmonitor=false"',
        'fenced["GIT_TERMINAL_PROMPT"] = "0"',
        'fenced["GIT_ALLOW_PROTOCOL"] = "none" if offline else _ALLOWED_PROTOCOLS',
    ),
    _GIT_CACHE: (
        "if bare_dir.is_dir() and not self._bare_trusted(bare_dir):",
        "and self._checkout_trusted_or_evicted(final_dir)",
        "and self._checkout_trusted(checkout_dir)",
        "self._checkout_trusted(final_dir)",
        "*inert_git_args(),",
    ),
    _SNAPSHOT: ("require_trusted_cached_repo(root, bare=False",),
}


def _name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _calls(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and _name(node.func) == name


def _carries(argv: ast.AST, helper: str) -> bool:
    return isinstance(argv, ast.List) and any(
        isinstance(item, ast.Starred) and _calls(item.value, helper) for item in argv.elts
    )


def _assigned(function: ast.AST, name: str) -> list[ast.AST]:
    return [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]


def _parameters(function: ast.AST) -> set[str]:
    args = getattr(function, "args", None)
    if args is None:
        return set()
    return {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _argv_ok(function: ast.AST, argv: ast.AST, helper: str) -> bool:
    if isinstance(argv, ast.Name):
        values = _assigned(function, argv.id)
        return bool(values) and _carries(values[0], helper)
    return _carries(argv, helper)


def _env_ok(function: ast.AST, env: ast.AST | None, line: int) -> bool:
    """The env is fenced at the call: a direct fence call, a parameter, or a name last set by one."""
    if env is None:
        return False
    if _calls(env, _ENV_FENCE):
        return True
    if not isinstance(env, ast.Name):
        return False
    earlier = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and node.lineno < line
        and any(isinstance(t, ast.Name) and t.id == env.id for t in node.targets)
    ]
    if not earlier:
        return env.id in _parameters(function)
    return _calls(max(earlier, key=lambda node: node.lineno).value, _ENV_FENCE)


def _functions(tree: ast.Module) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _innermost(functions: list[ast.AST], call: ast.Call) -> ast.AST | None:
    owners = [
        f
        for f in functions
        if f.lineno <= call.lineno <= (f.end_lineno or f.lineno)
        and any(n is call for n in ast.walk(f))
    ]
    return min(owners, key=lambda f: (f.end_lineno or f.lineno) - f.lineno, default=None)


def _check_site(path: str, source: str, helper: str, fence_env: bool) -> list[Violation]:
    findings: list[Violation] = []
    tree = ast.parse(source, filename=path)
    functions = _functions(tree)
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        callee = _name(call.func)
        if callee not in _RUNNERS and callee not in _ARG_RUNNERS:
            continue
        function = _innermost(functions, call) or tree
        keywords = {k.arg: k.value for k in call.keywords}
        if callee in _ARG_RUNNERS:
            extra = keywords.get("extra_git_args")
            if extra is None or not _calls(extra, helper):
                findings.append(
                    violation(_RULE_ID, path, f"{callee} must pass extra_git_args={helper}()")
                )
        elif not call.args or not _argv_ok(function, call.args[0], helper):
            findings.append(
                violation(_RULE_ID, path, f"git argv at line {call.lineno} must carry *{helper}()")
            )
        if fence_env and not _env_ok(function, keywords.get("env"), call.lineno):
            findings.append(
                violation(
                    _RULE_ID, path, f"git env at line {call.lineno} must come from {_ENV_FENCE}()"
                )
            )
    return findings


# Entry points into a cached bare that must open with the owner's trust gate.
_GATED = ("_bare_has_sha", "_fetch_into_bare_locked", "bare_ref_sha")
_GATE = "self._bare_trusted"
# Entry points that open a cached checkout, and the trust check each must call.
_CHECKOUT_GATES = {
    "_get_checkout": "self._checkout_trusted",
    "_read_only_checkout": "self._checkout_trusted",
    "_create_checkout": "self._checkout_trusted_or_evicted",
}


def _is_preamble(node: ast.AST) -> bool:
    """Imports, the docstring, and plain assignments that run no Git."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return True
    return isinstance(node, ast.Assign) and not any(
        isinstance(n, ast.Call) and _name(n.func) in _RUNNERS for n in ast.walk(node)
    )


def _opens_with_gate(function: ast.AST) -> bool:
    body = [node for node in getattr(function, "body", []) if not _is_preamble(node)]
    if not body or not isinstance(body[0], ast.If):
        return False
    gate = body[0]
    return any(_calls(n, _GATE) for n in ast.walk(gate.test)) and isinstance(
        gate.body[0], (ast.Return, ast.Raise)
    )


def _check_gates(path: str, source: str) -> list[Violation]:
    tree = ast.parse(source, filename=path)
    functions = {f.name: f for f in _functions(tree)}
    findings = [
        violation(_RULE_ID, path, f"{name} must open with a `{_GATE}(...)` check")
        for name in _GATED
        if name not in functions or not _opens_with_gate(functions[name])
    ]
    for name, gate in _CHECKOUT_GATES.items():
        function = functions.get(name)
        if function is None or not any(_calls(n, gate) for n in ast.walk(function)):
            findings.append(violation(_RULE_ID, path, f"{name} must call {gate}(...)"))
    return findings


def _check_cached_repo_git(provider: FactsProvider) -> tuple[Violation, ...]:
    """Git runs in persisted repositories are inert and gated by the owner's trust check."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    for path, (helper, fence_env) in _SITES.items():
        facts, failures = load_required(provider, inventory, _RULE_ID, path)
        if failures:
            findings.extend(failures)
            continue
        findings.extend(_check_site(path, source_text(facts), helper, fence_env))
        if path == _GIT_CACHE:
            findings.extend(_check_gates(path, source_text(facts)))
    for path, literals in _REQUIRED.items():
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RULE_ID,
                path,
                literals,
                "Persisted repositories must be trust-checked and run Git inertly",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_GIT_CACHE, "src/apm_cli/cache/paths.py"}),
            re.compile(
                r"\b(?:get_git_db_path|get_git_checkouts_path|GIT_DB_BUCKET|GIT_CHECKOUTS_BUCKET)\b"
            ),
            "Only cache/git_cache.py may address the git cache buckets",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_GIT_CACHE}),
            re.compile(r"\._db_root\b"),
            "Only cache/git_cache.py may open its bares (use GitCache.bare_ref_sha)",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            ("src/apm_cli/deps/github_downloader.py",),
            re.compile(r"\bvalidate_materialized_symlinks\b"),
            "Cached checkouts are link-checked through cached_repo.validate_cached_links",
            exempt=False,
        )
    )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Git runs in persisted repositories go through the cached-repo owner.",
        check=_check_cached_repo_git,
    ),
)
COLLECTORS: tuple[object, ...] = ()
