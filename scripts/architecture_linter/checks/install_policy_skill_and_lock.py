"""Skill-subset, locked-skill, update-plan, and marketplace-lock install
policy analyzers.

Ports six guard-less semantic rules (AC4 declared intent / AC7 mutation
locking), including the one AST-shaped skill-subset-token detector.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scripts.architecture_linter.checks.install_policy_shared import (
    _DEPS_LOCKFILE,
    _ELSE_TERMINATOR,
    _SKILL_INTEGRATOR,
    _TESTS_TREE,
    _after_context,
    _banned,
    _configured,
    _first_line,
    _has_text,
    _indent_scoped_branch,
    _matches,
    _numbered,
    _report,
    _require_all,
    _tree_python_paths,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts
from scripts.architecture_linter.models import Violation

RULE_SKILL_SUBSET = "install-deployment-skill-subset-tokens"


RULE_CLAUDE_SKILL = "install-deployment-cached-claude-skill-metadata"


RULE_LOCKED_SUBSET = "install-deployment-locked-skill-subset-reconstruction"


RULE_UPDATE_PLAN_REFS = "install-deployment-update-plan-ref-annotation"


RULE_GIT_OBJECT_FIELDS = "install-deployment-git-object-field-authority"


RULE_MARKETPLACE_LOCK = "install-deployment-marketplace-mutation-lock"


_SRC_TREE = "src/"


def _awk_body(
    lines: Sequence[tuple[int, str]],
    *,
    start: re.Pattern[str],
    boundary: re.Pattern[str],
    keep: re.Pattern[str] | None = None,
    hard_stop: re.Pattern[str] | None = None,
) -> tuple[tuple[int, str], ...]:
    """Capture a definition body the way the legacy block-capture awk did.

    Capture opens on the first line matching `start` (inclusive) and closes
    just before the next line matching `boundary` that does not also match
    `keep` -- the ``/start/{flag=1} flag&&/boundary/&&!/keep/{exit}`` idiom --
    or before the next line matching `hard_stop`, when one is supplied.
    """
    keep_pattern = keep if keep is not None else start
    body: list[tuple[int, str]] = []
    capturing = False
    for number, text in lines:
        if not capturing:
            if start.search(text) is None:
                continue
            capturing = True
            body.append((number, text))
            continue
        if hard_stop is not None and hard_stop.search(text) is not None:
            break
        if boundary.search(text) is not None and keep_pattern.search(text) is None:
            break
        body.append((number, text))
    return tuple(body)


def _class_slice(
    lines: Sequence[tuple[int, str]],
    *,
    start: re.Pattern[str],
    stop: re.Pattern[str],
) -> tuple[tuple[int, str], ...]:
    """Return the lines of one class block, as the legacy start/stop awk did."""
    inside = False
    body: list[tuple[int, str]] = []
    for number, text in lines:
        if start.search(text) is not None:
            inside = True
        if stop.search(text) is not None:
            inside = False
        if inside:
            body.append((number, text))
    return tuple(body)


_VALIDATION_OWNER = "src/apm_cli/models/validation.py"


_INSTALL_SOURCES = "src/apm_cli/install/sources.py"


_CLAUDE_SKILL_OWNER_DEF = re.compile(r"^def _validate_claude_skill\(")


_TOP_LEVEL_DEF = re.compile(r"^def ")


_CLAUDE_SKILL_OWNER_NEEDLES = ("load_frontmatter", 'version="unknown"')


_CACHED_SOURCE_CLASS = re.compile(r"^class CachedDependencySource\(")


_FRESH_SOURCE_CLASS = re.compile(r"^class FreshDependencySource\(")


_CLAUDE_SKILL_DISPATCH = "pkg_type == PackageType.CLAUDE_SKILL"


_CLAUDE_SKILL_BRANCH_START = re.compile(r"elif pkg_type == PackageType\.CLAUDE_SKILL:")


_NON_SPACE = re.compile(r"[^ ]")


_CLAUDE_SKILL_BRANCH_NEEDLES = (
    "validate_apm_package(install_path)",
    "not validation_result.is_valid or validation_result.package is None",
    "Cached Claude Skill is invalid",
)


_CLAUDE_SKILL_PARALLEL = re.compile(r"APMPackage\(|repo_url\.split")


def check_cached_claude_skill_metadata(provider: FactsProvider) -> tuple[Violation, ...]:
    """Cached/frozen Claude Skill lock metadata must route through validation.py."""
    rule_id = RULE_CLAUDE_SKILL
    owner, owner_fail = _configured(provider, _VALIDATION_OWNER, rule_id)
    consumer, consumer_fail = _configured(provider, _INSTALL_SOURCES, rule_id)
    failures = [*owner_fail, *consumer_fail]
    if failures:
        return tuple(failures)

    owner_body = _awk_body(
        owner,
        start=_CLAUDE_SKILL_OWNER_DEF,
        boundary=_TOP_LEVEL_DEF,
        keep=_CLAUDE_SKILL_OWNER_DEF,
    )
    findings = _require_all(
        rule_id,
        _VALIDATION_OWNER,
        owner_body,
        _CLAUDE_SKILL_OWNER_NEEDLES,
        "Claude Skill metadata owner (_validate_claude_skill) must derive lock "
        "metadata from frontmatter",
    )

    cached_body = _class_slice(consumer, start=_CACHED_SOURCE_CLASS, stop=_FRESH_SOURCE_CLASS)
    if not _has_text(cached_body, _CLAUDE_SKILL_DISPATCH):
        findings.append(
            _report(
                rule_id,
                _INSTALL_SOURCES,
                "CachedDependencySource must branch on the Claude Skill package type; "
                f"missing: {_CLAUDE_SKILL_DISPATCH}",
            )
        )
    branch = _indent_scoped_branch(
        cached_body,
        start=_CLAUDE_SKILL_BRANCH_START,
        terminator=_ELSE_TERMINATOR,
        probe=_NON_SPACE,
        include_start=True,
        restart_skips=False,
    )
    findings.extend(
        _require_all(
            rule_id,
            _INSTALL_SOURCES,
            branch,
            _CLAUDE_SKILL_BRANCH_NEEDLES,
            "Cached Claude Skill metadata must be validated through validation.py",
        )
    )
    findings.extend(
        _report(
            rule_id,
            _INSTALL_SOURCES,
            "Cached Claude Skill lock metadata must come from validation.py, not be "
            "reconstructed from a raw package or repo URL",
            line,
            column,
        )
        for line, column in _matches(branch, _CLAUDE_SKILL_PARALLEL, respect_exempt=False)
    )
    return tuple(findings)


_TO_DEPENDENCY_REF_DEF = re.compile(r"^    def to_dependency_ref\(")


_METHOD_BOUNDARY = re.compile(r"^    def ")


_TO_DEPENDENCY_REF_KEEP = re.compile(r"to_dependency_ref")


_CLASS_BOUNDARY = re.compile(r"^class ")


_LOCKED_SUBSET_NEEDLES = ("DependencyReference(", "skill_subset=", "self.skill_subset")


def check_locked_skill_subset(provider: FactsProvider) -> tuple[Violation, ...]:
    """LockedDependency.to_dependency_ref must reconstruct skill_subset."""
    rule_id = RULE_LOCKED_SUBSET
    lines, failures = _configured(provider, _DEPS_LOCKFILE, rule_id)
    if failures:
        return failures

    body = _awk_body(
        lines,
        start=_TO_DEPENDENCY_REF_DEF,
        boundary=_METHOD_BOUNDARY,
        keep=_TO_DEPENDENCY_REF_KEEP,
        hard_stop=_CLASS_BOUNDARY,
    )
    return tuple(
        _require_all(
            rule_id,
            _DEPS_LOCKFILE,
            body,
            _LOCKED_SUBSET_NEEDLES,
            "LockedDependency.to_dependency_ref must reconstruct skill_subset from "
            "self.skill_subset",
        )
    )


_REF_REUSE = "src/apm_cli/install/helpers/ref_reuse.py"


_ANNOTATE_REFS_DEF = re.compile(r"^def annotate_update_plan_refs\(")


_ANNOTATE_REFS_KEEP = re.compile(r"annotate_update_plan_refs")


_ANNOTATE_REFS_NEEDLES = (
    "downloader.resolve_git_reference(dep_ref)",
    "dep_ref.resolved_reference = resolved",
)


def check_update_plan_ref_annotation(provider: FactsProvider) -> tuple[Violation, ...]:
    """Cached update planning must resolve refs through the downloader owner."""
    rule_id = RULE_UPDATE_PLAN_REFS
    lines, failures = _configured(provider, _REF_REUSE, rule_id)
    if failures:
        return failures

    body = _awk_body(
        lines,
        start=_ANNOTATE_REFS_DEF,
        boundary=_TOP_LEVEL_DEF,
        keep=_ANNOTATE_REFS_KEEP,
    )
    return tuple(
        _require_all(
            rule_id,
            _REF_REUSE,
            body,
            _ANNOTATE_REFS_NEEDLES,
            "Cached update planning must resolve refs through the downloader owner",
        )
    )


_OBJECT_FIELDS_OWNER = "src/apm_cli/models/dependency/object_fields.py"


_DEPENDENCY_PARSER = "src/apm_cli/models/dependency/reference.py"


_LOCAL_PACKAGE_FIXTURE = "tests/utils/local_package.py"


_DEPENDENCY_PARSER_NEEDLES = (
    "reject_unknown_git_fields(entry, parent=True)",
    "reject_unknown_git_fields(entry, parent=False)",
)


_GIT_FIELD_DUPLICATES = re.compile(
    r"def reject_unknown_git_fields|_(REMOTE|PARENT)_GIT_DEPENDENCY_FIELDS"
)


_GIT_FIELD_FIXTURE = re.compile(r"reject_unknown_fields|_(REMOTE|PARENT)?_?GIT_DEPENDENCY_FIELDS")


def check_git_object_field_authority(provider: FactsProvider) -> tuple[Violation, ...]:
    """Object-form Git dependency fields must come from the product parser."""
    rule_id = RULE_GIT_OBJECT_FIELDS
    parser, parser_fail = _configured(provider, _DEPENDENCY_PARSER, rule_id)
    findings: list[Violation] = list(parser_fail)
    if not parser_fail:
        findings.extend(
            _require_all(
                rule_id,
                _DEPENDENCY_PARSER,
                parser,
                _DEPENDENCY_PARSER_NEEDLES,
                "Object-form Git dependency fields must be admitted by the product "
                "parser for both parent and child entries",
            )
        )

    scanned = (
        *_tree_python_paths(provider, _SRC_TREE, excluded=(_OBJECT_FIELDS_OWNER,)),
        *_tree_python_paths(provider, _TESTS_TREE),
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=scanned,
            pattern=_GIT_FIELD_DUPLICATES,
            message=(
                "Object-form Git dependency field vocabulary must come from "
                "models/dependency/object_fields.py"
            ),
            configured=False,
            respect_exempt=True,
        )
    )
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=(_LOCAL_PACKAGE_FIXTURE,),
            pattern=_GIT_FIELD_FIXTURE,
            message=(
                "Test fixtures must build dependency entries through the product "
                "parser instead of restating its field vocabulary"
            ),
            respect_exempt=True,
        )
    )
    return tuple(findings)


_MARKETPLACE_REGISTRY = "src/apm_cli/marketplace/registry.py"


_MARKETPLACE_MUTATION = "_marketplace_mutation"


_MARKETPLACE_MUTATORS = (
    ("def add_marketplace", 8),
    ("def remove_marketplace", 12),
)


def check_marketplace_mutation_lock(provider: FactsProvider) -> tuple[Violation, ...]:
    """Marketplace mutations must lock the full load-modify-save transaction."""
    rule_id = RULE_MARKETPLACE_LOCK
    lines, failures = _configured(provider, _MARKETPLACE_REGISTRY, rule_id)
    if failures:
        return failures

    findings: list[Violation] = []
    for anchor, context in _MARKETPLACE_MUTATORS:
        window = _after_context(lines, anchor, context)
        if not _has_text(window, _MARKETPLACE_MUTATION):
            findings.append(
                _report(
                    rule_id,
                    _MARKETPLACE_REGISTRY,
                    "Marketplace mutations must lock the full load-modify-save "
                    f"transaction; '{anchor}' does not enter {_MARKETPLACE_MUTATION}",
                    _first_line(lines, anchor),
                )
            )
    return tuple(findings)


_PLUGIN_EXPORTER = "src/apm_cli/bundle/plugin_exporter.py"


_SKILL_SUBSET_FILES: tuple[str, ...] = (_SKILL_INTEGRATOR, _PLUGIN_EXPORTER)


_SKILL_SUBSET_OWNER = "models/dependency/subsets.py::skill_subset_filter_tokens"


_SKILL_SUBSET_LEXICAL = re.compile(
    r"def _skill_subset_name_filter|set\(dep\.skill_subset\)|Path\(normalized_path\)\.name"
)


_LEAF_PATH_CALLEES = frozenset({"PurePosixPath", "PureWindowsPath", "PurePath", "Path"})


_SIGNAL_REPLACE = "replace"


_SIGNAL_LEAF_DIRECT = "leaf_direct"


_SIGNAL_LEAF_READ = "leaf_read"


_SIGNAL_LEAF_ASSIGN = "leaf_assign"


_SIGNAL_ADD = "add"


@dataclass(frozen=True)
class SkillSubsetSignal:
    """One normalization-algorithm signal, attributed to its owning function.

    ``owner_line``/``owner_name`` identify the function whose *own* scope the
    signal belongs to -- nested ``def``/``lambda`` scopes are attributed to
    themselves, so a duplicate inside a helper is never misattributed to its
    caller (or the other way around).
    """

    kind: str
    owner_line: int
    owner_name: str
    symbol: str
    line: int


def _is_backslash_to_slash_replace(node: ast.AST) -> bool:
    """Return whether `node` is a call shaped like ``X.replace("\\\\", "/")``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "replace"):
        return False
    if len(node.args) != 2:
        return False
    first, second = node.args
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return False
    if not (isinstance(second, ast.Constant) and isinstance(second.value, str)):
        return False
    return "\\" in first.value and second.value == "/"


def _is_leaf_path_call(node: ast.AST) -> bool:
    """Return whether `node` calls one of the leaf-path constructors."""
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    if isinstance(callee, ast.Name):
        return callee.id in _LEAF_PATH_CALLEES
    if isinstance(callee, ast.Attribute):
        return callee.attr in _LEAF_PATH_CALLEES
    return False


def _is_token_set_add(node: ast.AST) -> bool:
    """Return whether `node` is a call shaped like ``tokens.add(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "add"


def _skill_subset_signals(index: TreeIndex) -> tuple[SkillSubsetSignal, ...]:
    """Derive own-scope signals from the canonical precomputed tree index."""
    signals: list[SkillSubsetSignal] = []
    for function in index.functions():
        owner_line = getattr(function, "lineno", 1)
        owner_name = index.function_qualname(function)
        for node in index.own_scope(function):
            signals.extend(_signals_for_node(node, owner_line, owner_name))
    return tuple(signals)


def _signals_for_node(
    node: ast.AST,
    owner_line: int,
    owner_name: str,
) -> tuple[SkillSubsetSignal, ...]:
    """Return the skill-subset signals contributed by one indexed node."""
    line = getattr(node, "lineno", owner_line)
    if _is_backslash_to_slash_replace(node):
        return (SkillSubsetSignal(_SIGNAL_REPLACE, owner_line, owner_name, "", line),)
    if _is_token_set_add(node):
        return (SkillSubsetSignal(_SIGNAL_ADD, owner_line, owner_name, "", line),)
    if isinstance(node, ast.Attribute) and node.attr == "name":
        if _is_leaf_path_call(node.value):
            return (SkillSubsetSignal(_SIGNAL_LEAF_DIRECT, owner_line, owner_name, "", line),)
        if isinstance(node.value, ast.Name):
            return (
                SkillSubsetSignal(_SIGNAL_LEAF_READ, owner_line, owner_name, node.value.id, line),
            )
        return ()
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name) and _is_leaf_path_call(node.value):
            return (
                SkillSubsetSignal(_SIGNAL_LEAF_ASSIGN, owner_line, owner_name, target.id, line),
            )
    return ()


def _skill_subset_duplicators(signals: Iterable[object]) -> tuple[tuple[int, str], ...]:
    """Return every function whose own scope combines all three signals."""
    by_owner: dict[tuple[int, str], dict[str, set[str]]] = {}
    for signal in signals:
        if not isinstance(signal, SkillSubsetSignal):
            continue
        owner = (signal.owner_line, signal.owner_name)
        kinds = by_owner.setdefault(owner, {})
        kinds.setdefault(signal.kind, set()).add(signal.symbol)

    duplicators: list[tuple[int, str]] = []
    for owner, kinds in by_owner.items():
        leaf_vars = kinds.get(_SIGNAL_LEAF_ASSIGN, set())
        has_leaf = _SIGNAL_LEAF_DIRECT in kinds or bool(
            kinds.get(_SIGNAL_LEAF_READ, set()) & leaf_vars
        )
        if _SIGNAL_REPLACE in kinds and has_leaf and _SIGNAL_ADD in kinds:
            duplicators.append(owner)
    return tuple(sorted(duplicators))


def check_skill_subset_tokens(provider: FactsProvider) -> tuple[Violation, ...]:
    """Skill subset filter tokens must come from models/dependency/subsets.py."""
    rule_id = RULE_SKILL_SUBSET
    findings: list[Violation] = []
    for path in _SKILL_SUBSET_FILES:
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        lines = _numbered(facts)
        findings.extend(
            _report(
                rule_id,
                path,
                "Skill subset filter tokens must come from "
                f"{_SKILL_SUBSET_OWNER}, not a local filter",
                line,
                column,
            )
            for line, column in _matches(
                lines,
                _SKILL_SUBSET_LEXICAL,
                respect_exempt=True,
            )
        )
        index = provider.tree_index(path)
        signals = () if index is None else _skill_subset_signals(index)
        for owner_line, owner_name in _skill_subset_duplicators(signals):
            findings.append(
                _report(
                    rule_id,
                    path,
                    f"function '{owner_name}' reimplements the {_SKILL_SUBSET_OWNER} "
                    "normalization algorithm (slash normalization + path-leaf "
                    "extraction + token-set collection); call the owner instead",
                    owner_line,
                )
            )
    return tuple(findings)
