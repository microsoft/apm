"""GitLab-adapter, local-bundle, require-hashes, and winner-selection install
policy analyzers.

Ports five guard-less semantic rules (AC3 policy authorities / AC4 declared
intent).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from scripts.architecture_linter.checks.install_policy_shared import (
    _APM_RESOLVER,
    _POLICY_DISCOVERY,
    _banned,
    _configured,
    _count_re,
    _first_line,
    _indent_scoped_branch,
    _matches,
    _report,
    _require_all,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_GITLAB_ADAPTER = "install-deployment-gitlab-policy-adapter"


RULE_GITLAB_FACADE = "install-deployment-gitlab-facade-orchestration"


RULE_LOCAL_BUNDLE_PREFLIGHT = "install-deployment-local-bundle-policy-preflight"


RULE_REQUIRE_HASHES = "install-deployment-require-hashes-enforcement"


RULE_WINNER_SELECTION = "install-deployment-dependency-winner-selection"


RULE_REMOTE_ORIGIN_OWNER = "install-deployment-policy-remote-origin-owner"


def _count_text(lines: Sequence[tuple[int, str]], needle: str) -> int:
    """Return how many lines contain `needle` (``grep -Fc``)."""
    return sum(1 for _, text in lines if needle in text)


_GITLAB_ADAPTER = "src/apm_cli/policy/_gitlab.py"


_POLICY_TREE = "src/apm_cli/policy/"


_GITLAB_ADAPTER_DEFS = re.compile(
    r"^def (_fetch_from_gitlab_repo|_fetch_gitlab_contents"
    r"|_gitlab_project_state_via_git|_fetch_gitlab_chain_parent)\("
)


_GITLAB_FACADE_CALL = re.compile(r"_gitlab\._fetch_(from_gitlab_repo|gitlab_chain_parent)\(")


_GITLAB_ADAPTER_DEF_COUNT = 4


_GITLAB_FACADE_CALL_COUNT = 2


def check_gitlab_policy_adapter(provider: FactsProvider) -> tuple[Violation, ...]:
    """GitLab policy discovery must route through policy/_gitlab.py."""
    rule_id = RULE_GITLAB_ADAPTER
    adapter, adapter_fail = _configured(provider, _GITLAB_ADAPTER, rule_id)
    facade, facade_fail = _configured(provider, _POLICY_DISCOVERY, rule_id)
    failures = [*adapter_fail, *facade_fail]
    if failures:
        return tuple(failures)

    findings: list[Violation] = []
    definitions = _count_re(adapter, _GITLAB_ADAPTER_DEFS)
    if definitions != _GITLAB_ADAPTER_DEF_COUNT:
        findings.append(
            _report(
                rule_id,
                _GITLAB_ADAPTER,
                "GitLab policy adapter must define exactly "
                f"{_GITLAB_ADAPTER_DEF_COUNT} fetch/state helpers (found {definitions})",
            )
        )
    facade_calls = _count_re(facade, _GITLAB_FACADE_CALL)
    if facade_calls != _GITLAB_FACADE_CALL_COUNT:
        findings.append(
            _report(
                rule_id,
                _POLICY_DISCOVERY,
                "Policy discovery must delegate to the GitLab adapter exactly "
                f"{_GITLAB_FACADE_CALL_COUNT} times (found {facade_calls})",
            )
        )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _POLICY_TREE, excluded=(_GITLAB_ADAPTER,)),
            pattern=_GITLAB_ADAPTER_DEFS,
            message="Duplicate GitLab policy fetch helper; route through policy/_gitlab.py",
            configured=False,
            respect_exempt=True,
        )
    )
    return tuple(findings)


_REMOTE_ORIGIN_ARGV = re.compile(r'"remote",\s*"get-url",\s*"origin"')


_REMOTE_PARSER_DEFS = re.compile(
    r"^def (_remote_url_parts|_parse_remote_url|_git_remote_origin_url)\("
)


_REMOTE_PARSER_DEF_COUNT = 3


_REMOTE_ORIGIN_READ_COUNT = 1


def check_policy_remote_origin_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    """Reading and parsing the project git remote for policy discovery has one owner.

    ``discovery.py`` is the sole reader of ``git remote get-url origin`` and the
    sole home of the remote-URL splitter/parsers (``_remote_url_parts``,
    ``_parse_remote_url``, ``_git_remote_origin_url``). The owner MUST define all
    three helpers, and no other module in the policy tree may re-read or re-parse
    the remote -- either would reintroduce the double-read / divergent-parse the
    single-owner refactor removed (#2753).
    """
    rule_id = RULE_REMOTE_ORIGIN_OWNER
    owner, owner_fail = _configured(provider, _POLICY_DISCOVERY, rule_id)
    if owner_fail:
        return tuple(owner_fail)
    findings: list[Violation] = []
    definitions = _count_re(owner, _REMOTE_PARSER_DEFS)
    if definitions != _REMOTE_PARSER_DEF_COUNT:
        findings.append(
            _report(
                rule_id,
                _POLICY_DISCOVERY,
                "Policy discovery must define exactly "
                f"{_REMOTE_PARSER_DEF_COUNT} canonical git-remote read/parse helpers "
                f"(found {definitions})",
            )
        )
    origin_reads = _count_re(owner, _REMOTE_ORIGIN_ARGV)
    if origin_reads != _REMOTE_ORIGIN_READ_COUNT:
        findings.append(
            _report(
                rule_id,
                _POLICY_DISCOVERY,
                "Policy discovery must read the git remote origin exactly "
                f"{_REMOTE_ORIGIN_READ_COUNT} time via _git_remote_origin_url "
                f"(found {origin_reads} origin-read argv occurrences)",
            )
        )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _POLICY_TREE, excluded=(_POLICY_DISCOVERY,)),
            pattern=_REMOTE_ORIGIN_ARGV,
            message="Read the git remote origin only via discovery.py::_git_remote_origin_url",
            configured=False,
            respect_exempt=True,
        )
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=_tree_python_paths(provider, _POLICY_TREE, excluded=(_POLICY_DISCOVERY,)),
            pattern=_REMOTE_PARSER_DEFS,
            message="Remote-URL split/parse owner is discovery.py; do not redefine these helpers",
            configured=False,
            respect_exempt=True,
        )
    )
    return tuple(findings)


# The facade delegation now lives in ``_gitlab_walk_candidate`` (the GitLab
# branch of ``_auto_discover`` calls it; see #2753). Scope the orchestration
# scan to that helper's body -- from its ``def`` to the next top-level ``def``.
_GITLAB_WALK_START = re.compile(r"^def _gitlab_walk_candidate\(")


_TOP_LEVEL_DEF = re.compile(r"^def ")


_NON_WHITESPACE = re.compile(r"[^\s]")


_GITLAB_ORCHESTRATION = re.compile(
    r"(_read_cache_entry|_write_cache|requests\.|AuthResolver|subprocess\.run"
    r"|_fetch_gitlab_contents|_gitlab_project_state_via_git)"
)


def check_gitlab_facade_orchestration(provider: FactsProvider) -> tuple[Violation, ...]:
    """GitLab policy cache and transport must remain in policy/_gitlab.py."""
    rule_id = RULE_GITLAB_FACADE
    lines, failures = _configured(provider, _POLICY_DISCOVERY, rule_id)
    if failures:
        return failures

    branch = _indent_scoped_branch(
        lines,
        start=_GITLAB_WALK_START,
        terminator=_TOP_LEVEL_DEF,
        probe=_NON_WHITESPACE,
        include_start=False,
        restart_skips=True,
    )
    return tuple(
        _report(
            rule_id,
            _POLICY_DISCOVERY,
            "GitLab policy cache and transport must remain in policy/_gitlab.py; "
            "the facade branch must not orchestrate cache, transport, or auth",
            line,
            column,
        )
        for line, column in _matches(branch, _GITLAB_ORCHESTRATION, respect_exempt=False)
    )


_LOCAL_BUNDLE_HANDLER = "src/apm_cli/install/local_bundle_handler.py"


_LOCAL_BUNDLE_PREFLIGHT_NEEDLES = (
    "from ..policy.install_preflight import run_policy_preflight",
    "policy_fetch, _enforcement_active = run_policy_preflight(",
    "mcp_deps=bundle_mcp_deps",
)


def _has_cache_only_preflight(provider: FactsProvider) -> bool:
    """Return whether the sole canonical preflight call explicitly runs cache-only."""
    index = provider.tree_index(_LOCAL_BUNDLE_HANDLER)
    if index is None:
        return False
    calls = tuple(
        node
        for node in index.nodes
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_policy_preflight"
        )
    )
    return len(calls) == 1 and any(
        keyword.arg == "cache_only"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in calls[0].keywords
    )


def check_local_bundle_preflight(provider: FactsProvider) -> tuple[Violation, ...]:
    """Local bundle installs must route policy through install_preflight.py."""
    rule_id = RULE_LOCAL_BUNDLE_PREFLIGHT
    lines, failures = _configured(provider, _LOCAL_BUNDLE_HANDLER, rule_id)
    if failures:
        return failures
    findings = list(
        _require_all(
            rule_id,
            _LOCAL_BUNDLE_HANDLER,
            lines,
            _LOCAL_BUNDLE_PREFLIGHT_NEEDLES,
            "Local bundle installs must route policy through install_preflight.py",
        )
    )
    if not _has_cache_only_preflight(provider):
        findings.append(
            _report(
                rule_id,
                _LOCAL_BUNDLE_HANDLER,
                "Local bundle policy preflight must explicitly run cache-only",
            )
        )
    return tuple(findings)


_INSTALL_PIPELINE = "src/apm_cli/install/pipeline.py"


_POLICY_CHECKS = "src/apm_cli/policy/policy_checks.py"


_REQUIRE_HASHES = re.compile(r"policy(\.security\.integrity)?\.require_hashes")


def check_require_hashes_enforcement(provider: FactsProvider) -> tuple[Violation, ...]:
    """require_hashes enforcement must route through install/integrity.py."""
    return tuple(
        _banned(
            provider,
            rule_id=RULE_REQUIRE_HASHES,
            paths=(_INSTALL_PIPELINE, _LOCAL_BUNDLE_HANDLER, _POLICY_CHECKS),
            pattern=_REQUIRE_HASHES,
            message=(
                "require_hashes enforcement must route through install/integrity.py, "
                "not be re-read from policy here"
            ),
            respect_exempt=True,
        )
    )


_WINNER_LOCALS = re.compile(r"download_winners|level_winners|seen_keys|nodes_at_depth\.sort")


_WINNER_SELECTOR_CALL = "_select_dependency_winners("


_WINNER_SELECTOR_CALL_COUNT = 3


def check_dependency_winner_selection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Dependency ref winner selection must use one shared helper."""
    rule_id = RULE_WINNER_SELECTION
    lines, failures = _configured(provider, _APM_RESOLVER, rule_id)
    if failures:
        return failures

    findings: list[Violation] = [
        _report(
            rule_id,
            _APM_RESOLVER,
            "Dependency ref winner selection must use one helper, not local "
            "winner/seen-key bookkeeping",
            line,
            column,
        )
        for line, column in _matches(lines, _WINNER_LOCALS, respect_exempt=True)
    ]
    calls = _count_text(lines, _WINNER_SELECTOR_CALL)
    if calls != _WINNER_SELECTOR_CALL_COUNT:
        findings.append(
            _report(
                rule_id,
                _APM_RESOLVER,
                "Dependency dispatch and flattening must share _select_dependency_winners "
                f"(expected {_WINNER_SELECTOR_CALL_COUNT} call sites, found {calls})",
                _first_line(lines, _WINNER_SELECTOR_CALL),
            )
        )
    return tuple(findings)
