"""Single-process orchestrator for the architecture boundary linter.

`run()` is the one entry point: it builds the repository inventory, imports
exactly six rule-group modules, validates their combined rule catalog,
validates the canonical-owner registry bidirectionally against that catalog,
executes every rule exactly once, and returns an immutable
:class:`~scripts.architecture_linter.models.RunReport`. Nothing here ever
raises out of `run()` for a problem that can instead be reported: missing
groups, malformed rule catalogs, registry errors, individual rule exceptions,
and malformed rule results are all captured as
:class:`~scripts.architecture_linter.models.Failure` entries so the CLI can
aggregate everything into one deterministic report instead of crashing.

The six group-module imports are intentionally six separate, literal import
statements (not a loop over a name list) so a missing module fails exactly
where it is imported, with its own catchable `ImportError`, and every other
group's import is attempted independently.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

from scripts.architecture_linter.diagnostics import DiagnosticCollector
from scripts.architecture_linter.facts import Collector, FactsProvider
from scripts.architecture_linter.inventory import (
    EXCLUDED_ROOTS,
    build_inventory,
    is_safe_repository_relative_path,
)
from scripts.architecture_linter.models import (
    Failure,
    GroupResult,
    Rule,
    RuleResult,
    RunMetrics,
    RunReport,
    Violation,
)
from scripts.architecture_linter.process_audit import (
    begin_counting,
    child_process_count,
)
from scripts.architecture_linter.registry import (
    OwnerRegistry,
    RegistryError,
    load_registry_documents,
    validate_shard_name,
)

# The exact, ordered set of rule-group modules this engine hosts. Order is
# fixed (not alphabetized) and drives every deterministic iteration below.
GROUP_MODULE_NAMES: tuple[str, ...] = (
    "registry_delegation",
    "mutation_writes",
    "contracts_tests",
    "install_deployment",
    "transport_platform",
    "marketplace_integrations",
)

_GroupImport = tuple[str, ModuleType | None, str | None]

# Test-only injection seam. Real group imports stay lazy so importing this
# module cannot execute group code before the process audit begins.
_GROUP_IMPORTS: tuple[_GroupImport, ...] | None = None

_OWNERS_DIR = ".apm/architecture/owners"
_OWNERS_INDEX = f"{_OWNERS_DIR}/index.json"
_SEMANTIC_RULE_ID = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")


def _import_groups() -> tuple[_GroupImport, ...]:
    """Attempt all six literal imports after process auditing has begun."""
    try:
        from scripts.architecture_linter.groups import registry_delegation
    except (Exception, SystemExit) as exc:
        registry_delegation = None
        registry_delegation_error: str | None = str(exc)
    else:
        registry_delegation_error = None

    try:
        from scripts.architecture_linter.groups import mutation_writes
    except (Exception, SystemExit) as exc:
        mutation_writes = None
        mutation_writes_error: str | None = str(exc)
    else:
        mutation_writes_error = None

    try:
        from scripts.architecture_linter.groups import contracts_tests
    except (Exception, SystemExit) as exc:
        contracts_tests = None
        contracts_tests_error: str | None = str(exc)
    else:
        contracts_tests_error = None

    try:
        from scripts.architecture_linter.groups import install_deployment
    except (Exception, SystemExit) as exc:
        install_deployment = None
        install_deployment_error: str | None = str(exc)
    else:
        install_deployment_error = None

    try:
        from scripts.architecture_linter.groups import transport_platform
    except (Exception, SystemExit) as exc:
        transport_platform = None
        transport_platform_error: str | None = str(exc)
    else:
        transport_platform_error = None

    try:
        from scripts.architecture_linter.groups import marketplace_integrations
    except (Exception, SystemExit) as exc:
        marketplace_integrations = None
        marketplace_integrations_error: str | None = str(exc)
    else:
        marketplace_integrations_error = None

    return (
        ("registry_delegation", registry_delegation, registry_delegation_error),
        ("mutation_writes", mutation_writes, mutation_writes_error),
        ("contracts_tests", contracts_tests, contracts_tests_error),
        ("install_deployment", install_deployment, install_deployment_error),
        ("transport_platform", transport_platform, transport_platform_error),
        ("marketplace_integrations", marketplace_integrations, marketplace_integrations_error),
    )


def _group_imports() -> tuple[_GroupImport, ...]:
    """Return injected test groups or perform the six audited real imports."""
    return _GROUP_IMPORTS if _GROUP_IMPORTS is not None else _import_groups()


def _is_printable_ascii(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(32 <= ord(character) <= 126 for character in value)
    )


class _CatalogEntry:
    """One group's validated rules, or the failure that disqualified it."""

    __slots__ = ("collectors", "failures", "group", "module", "rules")

    def __init__(self, group: str, module: ModuleType | None) -> None:
        self.group = group
        self.module = module
        self.rules: tuple[Rule, ...] = ()
        self.collectors: tuple[Collector, ...] = ()
        self.failures: list[Failure] = []


class RuleCatalogError(ValueError):
    """Raised when callers request an invalid static rule catalog."""


def _collect_group(
    group_name: str, module: ModuleType | None, import_error: str | None
) -> _CatalogEntry:
    entry = _CatalogEntry(group_name, module)
    if import_error is not None:
        entry.failures.append(Failure(f"import:{group_name}", import_error))
        return entry

    rules_attr = getattr(module, "RULES", None)
    if not isinstance(rules_attr, tuple) or not all(isinstance(item, Rule) for item in rules_attr):
        entry.failures.append(
            Failure(f"group:{group_name}", "RULES must be a tuple of Rule instances")
        )
        return entry

    valid_rules: list[Rule] = []
    for rule in rules_attr:
        metadata_errors: list[str] = []
        if not isinstance(rule.id, str):
            metadata_errors.append(f"rule id must be a string: {rule.id!r}")
        if not isinstance(rule.group, str):
            metadata_errors.append(f"rule {rule.id!r} group must be a string")
        if not isinstance(rule.description, str):
            metadata_errors.append(f"rule {rule.id!r} description must be a string")
        if not isinstance(rule.guard_ids, tuple) or not all(
            isinstance(guard_id, str) for guard_id in rule.guard_ids
        ):
            metadata_errors.append(f"rule {rule.id!r} guard_ids must be a tuple of strings")
        if not callable(rule.check):
            metadata_errors.append(f"rule {rule.id!r} check must be callable")
        for message in metadata_errors:
            entry.failures.append(Failure(f"group:{group_name}", message))
        if metadata_errors:
            continue

        if rule.group != group_name:
            entry.failures.append(
                Failure(
                    f"group:{group_name}",
                    f"rule {rule.id!r} declares mismatched group {rule.group!r}",
                )
            )
            continue
        if _SEMANTIC_RULE_ID.fullmatch(rule.id) is None:
            entry.failures.append(
                Failure(
                    f"group:{group_name}",
                    f"rule has invalid stable semantic ID: {rule.id!r}",
                )
            )
            continue
        if not _is_printable_ascii(rule.description):
            entry.failures.append(
                Failure(
                    f"group:{group_name}",
                    f"rule {rule.id!r} has a non-printable description",
                )
            )
            continue
        valid_rules.append(rule)
    entry.rules = tuple(sorted(valid_rules, key=lambda rule: rule.id))

    collectors_attr = getattr(module, "COLLECTORS", ())
    if isinstance(collectors_attr, tuple):
        entry.collectors = tuple(collectors_attr)
    else:
        entry.failures.append(
            Failure(f"group:{group_name}", "COLLECTORS must be a tuple of collector instances")
        )
    return entry


def _cross_group_failures(entries: Sequence[_CatalogEntry]) -> tuple[list[Failure], set[str]]:
    """Detect duplicate rule IDs across groups; return failures and the IDs to drop."""
    failures: list[Failure] = []
    id_counts = Counter(rule.id for entry in entries for rule in entry.rules)
    duplicate_ids = {rule_id for rule_id, count in id_counts.items() if count > 1}
    for rule_id in sorted(duplicate_ids):
        failures.append(Failure("rules", f"duplicate rule id across groups: {rule_id}"))
    return failures, duplicate_ids


def registered_rules() -> tuple[Rule, ...]:
    """Return the validated, deterministic rule catalog for test/tooling APIs.

    The normal CLI uses :func:`run`, which aggregates catalog failures.  This
    accessor is intentionally strict for tests that assert registry/API
    contracts without inspecting implementation source.
    """
    begin_counting()
    entries = [_collect_group(name, module, error) for name, module, error in _group_imports()]
    failures = [failure for entry in entries for failure in entry.failures]
    cross_failures, duplicate_ids = _cross_group_failures(entries)
    failures.extend(cross_failures)
    rules = tuple(rule for entry in entries for rule in entry.rules if rule.id not in duplicate_ids)
    duplicate_guards = _duplicate_guard_ids(rules)
    failures.extend(
        Failure("rules", f"duplicate guard id across rules: {guard_id}")
        for guard_id in duplicate_guards
    )
    observed_child_processes = child_process_count()
    if observed_child_processes:
        failures.append(
            Failure(
                "process",
                f"child-process events are forbidden (observed {observed_child_processes})",
            )
        )
    if failures:
        details = "; ".join(
            f"{failure.stage}: {failure.message}"
            for failure in sorted(failures, key=lambda item: (item.stage, item.message))
        )
        raise RuleCatalogError(details)
    return tuple(sorted(rules, key=lambda rule: rule.id))


def _duplicate_guard_ids(rules: Sequence[Rule]) -> tuple[str, ...]:
    guard_counts = Counter(guard for rule in rules for guard in rule.guard_ids)
    return tuple(sorted(guard for guard, count in guard_counts.items() if count > 1))


def _discover_shard_names(index_text: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(index_text)
    except json.JSONDecodeError:
        return ()
    shards = parsed.get("shards") if isinstance(parsed, dict) else None
    if not isinstance(shards, list):
        return ()
    names = tuple(name for name in shards if isinstance(name, str))
    for name in names:
        validate_shard_name(name)
    return names


def _load_registry(
    provider: FactsProvider,
    known_guard_ids: frozenset[str],
    owner_specific_guard_ids: frozenset[str],
) -> tuple[OwnerRegistry | None, Failure | None]:
    """Load the owner registry using bytes already fetched by `provider`.

    Never issues a second disk read for a file the inventory pass already
    read: every JSON document is pulled through `provider.source_cache`,
    which memoizes reads, and any extra on-disk shard is discovered from the
    inventory list already held in memory rather than a fresh directory scan.
    """
    index_text, index_error = provider.source_cache.read(_OWNERS_INDEX)
    if index_text is None:
        return None, Failure("registry", f"cannot read {_OWNERS_INDEX}: {index_error}")
    try:
        index_bytes = index_text.encode("ascii")
    except UnicodeEncodeError as exc:
        return None, Failure("registry", f"{_OWNERS_INDEX} is not printable ASCII: {exc}")

    owners_dir_prefix = f"{_OWNERS_DIR}/"
    disk_json_names = {
        path[len(owners_dir_prefix) :]
        for path in provider.inventory
        if path.startswith(owners_dir_prefix)
        and path.lower().endswith(".json")
        and path != _OWNERS_INDEX
    }
    try:
        listed_names = set(_discover_shard_names(index_text))
        for shard_name in disk_json_names:
            validate_shard_name(shard_name)
    except RegistryError as exc:
        return None, Failure("registry", str(exc))

    shard_contents: dict[str, bytes] = {}
    read_errors: list[str] = []
    for shard_name in sorted(listed_names | disk_json_names):
        shard_path = f"{_OWNERS_DIR}/{shard_name}"
        shard_text, shard_error = provider.source_cache.read(shard_path)
        if shard_text is None:
            read_errors.append(f"cannot read {shard_path}: {shard_error}")
            continue
        try:
            shard_contents[shard_name] = shard_text.encode("ascii")
        except UnicodeEncodeError as exc:
            read_errors.append(f"{shard_path} is not printable ASCII: {exc}")
    if read_errors:
        return None, Failure("registry", "; ".join(read_errors))

    try:
        registry = load_registry_documents(
            index_bytes,
            shard_contents,
            provider.inventory,
            known_guard_ids=known_guard_ids,
            owner_specific_guard_ids=owner_specific_guard_ids,
        )
    except RegistryError as exc:
        return None, Failure("registry", str(exc))
    return registry, None


def _validate_result(rule: Rule, raw_result: object) -> tuple[Violation, ...] | str:
    """Return validated violations, or an error string if malformed."""
    if raw_result is None:
        return "check() returned None instead of an iterable of Violation instances"
    if isinstance(
        raw_result,
        (str, bytes, bytearray, memoryview, Mapping, set, frozenset),
    ):
        return f"check() returned an invalid result container: {type(raw_result).__name__}"
    if not isinstance(raw_result, Iterable):
        return "check() did not return an iterable"
    try:
        items = list(raw_result)
    except (Exception, SystemExit) as exc:
        return f"check() result iteration raised {type(exc).__name__}: {exc}"

    violations: list[Violation] = []
    errors: list[str] = []
    for item in items:
        if not isinstance(item, Violation):
            errors.append(f"check() yielded a non-Violation item: {item!r}")
            continue
        if not isinstance(item.rule_id, str):
            errors.append(f"violation rule_id must be a string: {item.rule_id!r}")
            continue
        if item.rule_id != rule.id:
            errors.append(f"violation rule_id {item.rule_id!r} does not match rule {rule.id!r}")
            continue
        if not isinstance(item.path, str):
            errors.append(f"violation path must be a string: {item.path!r}")
            continue
        if not is_safe_repository_relative_path(item.path):
            errors.append(
                f"violation path must be a safe repository-relative POSIX path: {item.path!r}"
            )
            continue
        if type(item.line) is not int or type(item.column) is not int:
            errors.append(f"violation line/column must be integers: {item.line!r}:{item.column!r}")
            continue
        if item.line < 1 or item.column < 1:
            errors.append(f"violation has non-positive line/column: {item.line}:{item.column}")
            continue
        if not isinstance(item.message, str):
            errors.append(f"violation message must be a string: {item.message!r}")
            continue
        if not _is_printable_ascii(item.message):
            errors.append(f"violation message must be non-empty ASCII: {item.message!r}")
            continue
        violations.append(item)
    if errors:
        return "; ".join(errors)
    return tuple(violations)


def _execute_group(
    group_name: str,
    rules: Sequence[Rule],
    provider: FactsProvider,
    collector: DiagnosticCollector,
    guard_hits: Counter[str],
) -> GroupResult:
    started = time.perf_counter()
    results: list[RuleResult] = []
    for rule in rules:
        try:
            raw_result = rule.check(provider)
            validated = _validate_result(rule, raw_result)
        except (Exception, SystemExit) as exc:  # one rule's crash must not abort the run.
            collector.add_failure(Failure(f"rule:{rule.id}", f"raised: {exc}"))
            results.append(RuleResult(rule.id, group_name, rule.guard_ids, (), str(exc)))
            continue

        if isinstance(validated, str):
            collector.add_failure(Failure(f"rule-result:{rule.id}", validated))
            results.append(RuleResult(rule.id, group_name, rule.guard_ids, (), validated))
            continue

        for violation in validated:
            collector.add_violation(violation)
        for guard_id in rule.guard_ids:
            guard_hits[guard_id] += 1
        results.append(RuleResult(rule.id, group_name, rule.guard_ids, validated, None))

    duration = time.perf_counter() - started
    return GroupResult(
        group=group_name,
        module_name=group_name,
        import_error=None,
        rules=tuple(results),
        duration_seconds=duration,
    )


def _run(
    root: Path,
    *,
    selected_rule_ids: Collection[str] | None = None,
    source_overrides: Mapping[str, str | bytes] | None = None,
) -> RunReport:
    """Run the engine in full-catalog or explicitly incomplete test mode.

    `selected_rule_ids`, when given, restricts which rules *execute* to a
    Python-test-only subset; it never affects the deterministic inventory or
    the bidirectional registry validation, which always run against the full
    catalog. When restricted, the "every registered owner guard executes
    exactly once" invariant is skipped, since a deliberately partial run
    cannot satisfy a whole-repository guarantee.

    `source_overrides` is an in-memory, Python-only mutation-test seam.  It is
    routed through the same source cache and has no command-line equivalent.
    """
    begin_counting()
    start = time.perf_counter()
    collector = DiagnosticCollector()
    group_imports = _group_imports()

    try:
        inventory = build_inventory(root)
    except Exception as exc:  # startup failure must not crash the CLI.
        collector.add_failure(Failure("inventory", str(exc)))
        metrics = RunMetrics(
            inventory_file_count=0,
            excluded_root_count=len(EXCLUDED_ROOTS),
            read_attempts=0,
            read_successes=0,
            read_errors=0,
            max_reads_per_file=0,
            parse_attempts=0,
            parse_successes=0,
            parse_errors=0,
            max_parses_per_file=0,
            ast_visits=0,
            tree_index_builds=0,
            tree_index_cache_hits=0,
            max_tree_index_builds_per_file=0,
            peak_tree_index_nodes=0,
            per_group_seconds=tuple((name, 0.0) for name in GROUP_MODULE_NAMES),
            total_seconds=time.perf_counter() - start,
            child_process_count=child_process_count(),
        )
        return RunReport(
            violations=collector.violations,
            failures=collector.failures,
            group_results=(),
            metrics=metrics,
            exit_code=2 if selected_rule_ids is not None else 1,
        )

    entries = [_collect_group(name, module, error) for name, module, error in group_imports]
    for entry in entries:
        for failure in entry.failures:
            collector.add_failure(failure)

    cross_failures, duplicate_ids = _cross_group_failures(entries)
    for failure in cross_failures:
        collector.add_failure(failure)

    runnable_by_group: dict[str, tuple[Rule, ...]] = {}
    all_collectors: list[Collector] = []
    for entry in entries:
        survivors = tuple(rule for rule in entry.rules if rule.id not in duplicate_ids)
        runnable_by_group[entry.group] = survivors
        all_collectors.extend(entry.collectors)

    all_runnable_rules = [rule for rules in runnable_by_group.values() for rule in rules]
    duplicate_guards = _duplicate_guard_ids(all_runnable_rules)
    for guard_id in duplicate_guards:
        collector.add_failure(Failure("rules", f"duplicate guard id across rules: {guard_id}"))

    provider = FactsProvider(
        root,
        inventory.files,
        registry=None,
        collectors=tuple(all_collectors),
        source_overrides=source_overrides,
    )
    registry: OwnerRegistry | None = None
    if not duplicate_guards:
        guard_ids = frozenset(guard for rule in all_runnable_rules for guard in rule.guard_ids)
        registry, registry_failure = _load_registry(provider, guard_ids, guard_ids)
        if registry_failure is not None:
            collector.add_failure(registry_failure)
        provider.registry = registry
    else:
        collector.add_failure(
            Failure(
                "registry", "skipped: duplicate guard ids make owner-registry validation unsafe"
            )
        )

    execution_by_group = runnable_by_group
    if selected_rule_ids is not None:
        selected = frozenset(selected_rule_ids)
        unknown_selected = sorted(selected - {rule.id for rule in all_runnable_rules})
        if unknown_selected:
            collector.add_failure(
                Failure(
                    "selection",
                    "unknown selected rule IDs: " + ", ".join(unknown_selected),
                )
            )
        execution_by_group = {
            group: tuple(rule for rule in rules if rule.id in selected)
            for group, rules in runnable_by_group.items()
        }

    guard_hits: Counter[str] = Counter()
    # A group's rules were already reduced to the trustworthy survivors in
    # `_collect_group` / `runnable_by_group` (empty when the import failed or
    # RULES itself was malformed, filtered when only some rules mismatched
    # their declared group). Execution always runs against that survivor set
    # -- there is no separate "broken group" branch to keep in sync with it.
    group_results: list[GroupResult] = []
    for group_name in GROUP_MODULE_NAMES:
        entry = next(e for e in entries if e.group == group_name)
        result = _execute_group(
            group_name,
            execution_by_group.get(group_name, ()),
            provider,
            collector,
            guard_hits,
        )
        if entry.module is None:
            import_error = next(
                (f.message for f in entry.failures if f.stage == f"import:{group_name}"),
                None,
            )
            result = GroupResult(
                group=result.group,
                module_name=result.module_name,
                import_error=import_error,
                rules=result.rules,
                duration_seconds=result.duration_seconds,
            )
        group_results.append(result)

    if selected_rule_ids is None and registry is not None and not duplicate_guards:
        registered_guard_ids = {guard for owner in registry.owners for guard in owner.guards}
        for guard_id in sorted(registered_guard_ids):
            count = guard_hits.get(guard_id, 0)
            if count != 1:
                collector.add_failure(
                    Failure(
                        "guard",
                        f"owner guard did not execute exactly once: {guard_id} (count={count})",
                    )
                )

    for path, message in provider.source_cache.errors:
        collector.add_failure(Failure(f"read:{path}", message))
    for path, message in provider.parse_cache.errors:
        collector.add_failure(Failure(f"parse:{path}", message))

    observed_child_processes = child_process_count()
    if observed_child_processes:
        collector.add_failure(
            Failure(
                "process",
                f"child-process events are forbidden (observed {observed_child_processes})",
            )
        )

    total_seconds = time.perf_counter() - start
    per_group_seconds = tuple((result.group, result.duration_seconds) for result in group_results)
    metrics = RunMetrics(
        inventory_file_count=len(inventory.files),
        excluded_root_count=len(inventory.excluded_roots),
        read_attempts=provider.source_cache.read_attempts,
        read_successes=provider.source_cache.read_successes,
        read_errors=provider.source_cache.read_errors,
        max_reads_per_file=provider.source_cache.max_reads_per_file,
        parse_attempts=provider.parse_cache.parse_attempts,
        parse_successes=provider.parse_cache.parse_successes,
        parse_errors=provider.parse_cache.parse_errors,
        max_parses_per_file=provider.parse_cache.max_parses_per_file,
        ast_visits=provider.ast_visits,
        tree_index_builds=provider.tree_index_builds,
        tree_index_cache_hits=provider.tree_index_cache_hits,
        max_tree_index_builds_per_file=provider.max_tree_index_builds_per_file,
        peak_tree_index_nodes=provider.peak_tree_index_nodes,
        per_group_seconds=per_group_seconds,
        total_seconds=total_seconds,
        child_process_count=observed_child_processes,
    )

    exit_code = 2 if selected_rule_ids is not None else 1 if collector.has_findings else 0
    return RunReport(
        violations=collector.violations,
        failures=collector.failures,
        group_results=tuple(group_results),
        metrics=metrics,
        exit_code=exit_code,
    )


def run(root: Path) -> RunReport:
    """Run the complete six-group catalog against `root`."""
    return _run(root)


def run_selected_rules(
    root: Path,
    rule_ids: Collection[str],
    *,
    source_overrides: Mapping[str, str | bytes] | None = None,
) -> RunReport:
    """Run a test-only rule subset against a repository sandbox.

    This API deliberately has no CLI counterpart.  CI entrypoints always run
    the complete catalog, while mutation tests can exercise one authority
    against optional in-memory source overrides without paying for every
    unrelated rule. Its exit code is always ``2`` ("incomplete"), even when the
    selected rules report no findings, so it can never masquerade as a clean
    full lint.
    """
    return _run(
        root,
        selected_rule_ids=rule_ids,
        source_overrides=source_overrides,
    )
