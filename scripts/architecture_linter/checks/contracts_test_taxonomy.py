"""Behavioral-test-taxonomy and manifest-shape contract/test analyzers.

Ports five owner guards recorded in
``.apm/architecture/owners/hooks-integrations.json`` (each rule id equals
its guard id) plus one guard-less structural rule (lifecycle-partition
contract), and splices in the legacy-shell blocks restored in
:mod:`scripts.architecture_linter.checks.contracts_legacy`: AC14 ADO lock
coordinates, the tests/ half of the ref-recheck owner guard, and the
object-form Git dependency field duplicate/fixture scans. Each spliced-in
entry is a guard-less structural rule, so the five owner guards above keep
their single registry allocation.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence

from scripts.architecture_linter.checks.contracts_generation_footer import (
    check_generation_footer_authority,
)
from scripts.architecture_linter.checks.contracts_legacy import LEGACY_CHECKS
from scripts.architecture_linter.checks.contracts_structural_authorities import (
    find_binary_selection_violations,
    find_ratchet_authority_violations,
    find_rendered_parity_violations,
)
from scripts.architecture_linter.checks.contracts_test_shared import (
    _lines,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.checks.lexical_shared import (
    body_has as _body_has,
)
from scripts.architecture_linter.checks.lexical_shared import (
    body_has_regex as _body_has_re,
)
from scripts.architecture_linter.checks.lexical_shared import (
    captured_facts_body as _awk_body,
)
from scripts.architecture_linter.checks.lexical_shared import (
    duplicate_definition_lines as _duplicate_definition_lines,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

GROUP = "contracts_tests"


_GUARD_TAXONOMY = "contracts-tests-taxonomy-classification"


_GUARD_DEPENDENCY_IDENTITY = "contracts-tooling-dependency-identity"


_GUARD_CACHED_POLICY = "contracts-tooling-cached-policy-shape"


_GUARD_APPLY_TO = "contracts-tooling-apply-to-placement"


_GUARD_FRONTMATTER = "contracts-tooling-frontmatter-yaml"


_GUARD_LOCKFILE_READ = "contracts-tooling-lockfile-read"


_GUARD_GENERATION_FOOTER = "contracts-tooling-generation-footer"


_SRC_PREFIX = "src/apm_cli/"

_LOCKFILE_OWNER = "src/apm_cli/deps/lockfile.py"

_LOCKFILE_CONSUMERS = (
    "src/apm_cli/bundle/packer.py",
    "src/apm_cli/bundle/plugin_exporter.py",
    "src/apm_cli/bundle/agent_plugin_exporter.py",
)


def _facts_for(provider: FactsProvider, path: str, rule_id: str):
    """Return ``(facts, failures)`` for one Python owner/consumer file."""
    return checked_facts(provider, path, rule_id, require_python=path.endswith(".py"))


def _present_re(facts: object, pattern: re.Pattern[str]) -> bool:
    """Return whether any single line matches `pattern` (grep -Eq)."""
    return any(pattern.search(line) is not None for line in _lines(facts))


def _count_re(facts: object, pattern: re.Pattern[str]) -> int:
    """Return how many lines match `pattern` (grep -Ec)."""
    return sum(1 for line in _lines(facts) if pattern.search(line) is not None)


def _line_findings(
    facts: object,
    path: str,
    rule_id: str,
    pattern: re.Pattern[str],
    message: str,
    *,
    respect_exempt: bool,
) -> list[Violation]:
    """Report every matching line in one file (check_pattern semantics)."""
    findings: list[Violation] = []
    for number, line in enumerate(_lines(facts), start=1):
        if respect_exempt and EXEMPT_MARKER in line:
            continue
        match = pattern.search(line)
        if match is not None:
            findings.append(
                violation(rule_id, path, message, line=number, column=match.start() + 1)
            )
    return findings


def _count_defs_across(provider: FactsProvider, prefix: str, pattern: re.Pattern[str]) -> int:
    """Count lines matching `pattern` across every Python file under `prefix`."""
    total = 0
    for path in _python_paths(provider, prefix):
        facts = provider.file_facts(path)
        if getattr(facts, "read_error", None) is not None:
            continue
        total += _count_re(facts, pattern)
    return total


def _named_calls(nodes: Sequence[ast.AST], name: str) -> tuple[ast.Call, ...]:
    """Return direct calls to one unqualified function name."""
    return tuple(
        node
        for node in nodes
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    )


def _keyword_is_name(call: ast.Call, keyword_name: str, value_name: str) -> bool:
    """Return whether a call has `keyword_name=value_name`."""
    return any(
        keyword.arg == keyword_name
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == value_name
        for keyword in call.keywords
    )


def check_lockfile_read_resolution(provider: FactsProvider) -> tuple[Violation, ...]:
    """Read-only lockfile consumers must route through one non-mutating owner."""
    rule_id = _GUARD_LOCKFILE_READ
    owner, owner_failures = _facts_for(provider, _LOCKFILE_OWNER, rule_id)
    if owner_failures:
        return tuple(owner_failures)
    owner_index = owner.tree_index
    if owner_index is None:
        return (_summary(rule_id, _LOCKFILE_OWNER, "Lockfile owner has no Python syntax tree"),)

    findings: list[Violation] = []
    resolver = owner_index.function("resolve_lockfile_path_for_read")
    if resolver is None:
        findings.append(
            _summary(rule_id, _LOCKFILE_OWNER, "Read-only lockfile resolver must have one owner")
        )
    else:
        read_only_guards = tuple(
            node
            for node in owner_index.children(resolver)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "read_only"
        )
        migrate_calls = _named_calls(
            owner_index.own_scope(resolver),
            "migrate_lockfile_if_needed",
        )
        migration_is_read_only = bool(read_only_guards) and any(
            call in owner_index.walk(read_only_guards[0]) for call in migrate_calls
        )
        if len(read_only_guards) != 1 or len(migrate_calls) != 1 or migration_is_read_only:
            findings.append(
                _summary(
                    rule_id,
                    _LOCKFILE_OWNER,
                    "Read-only lockfile resolution must guard migration",
                )
            )

    installed_paths = owner_index.function("LockFile.installed_paths_for_project")
    if installed_paths is None:
        findings.append(
            _summary(rule_id, _LOCKFILE_OWNER, "LockFile installed-path reader must exist")
        )
    else:
        installed_nodes = owner_index.own_scope(installed_paths)
        installed_calls = _named_calls(installed_nodes, "resolve_lockfile_path_for_read")
        rederives_legacy = any(
            isinstance(node, ast.Name) and node.id == "LEGACY_LOCKFILE_NAME"
            for node in installed_nodes
        )
        has_read_only_call = len(installed_calls) == 1 and any(
            keyword.arg == "read_only"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in installed_calls[0].keywords
        )
        if not has_read_only_call or rederives_legacy:
            findings.append(
                _summary(
                    rule_id,
                    _LOCKFILE_OWNER,
                    "LockFile installed-path reads must delegate without re-deriving fallback",
                )
            )

    for consumer_path in _LOCKFILE_CONSUMERS:
        consumer, consumer_failures = _facts_for(provider, consumer_path, rule_id)
        findings.extend(consumer_failures)
        if consumer_failures or consumer.tree_index is None:
            continue
        imported = {
            alias.name
            for node in consumer.tree_index.nodes
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        calls = _named_calls(
            consumer.tree_index.nodes,
            "resolve_lockfile_path_for_read",
        )
        routes_read_only = len(calls) == 1 and _keyword_is_name(
            calls[0],
            "read_only",
            "dry_run",
        )
        if (
            "resolve_lockfile_path_for_read" not in imported
            or {"get_lockfile_path", "migrate_lockfile_if_needed"} & imported
            or not routes_read_only
        ):
            findings.append(
                _summary(
                    rule_id,
                    consumer_path,
                    "Bundle lockfile reads must route through the read-only owner",
                )
            )
    return tuple(findings)


_TAXONOMY_PLUGIN = "tests/quality/taxonomy_inventory_plugin.py"


_TAXONOMY_CONTRACT = "tests/quality/test_test_taxonomy.py"


_TAXONOMY_PARALLEL = re.compile(
    r"(^|[^A-Za-z_])(MANIFEST|_manifest_modules|tracked_python_inventory)"
    r"|behavioral markers outside critical manifest"
    r"|len\(modules\)[ \t]*=="
)


def check_taxonomy_classification(provider: FactsProvider) -> tuple[Violation, ...]:
    """Behavioral test taxonomy must stay owned by module-level pytestmark."""
    rule_id = _GUARD_TAXONOMY
    plugin, plugin_fail = _facts_for(provider, _TAXONOMY_PLUGIN, rule_id)
    contract, contract_fail = _facts_for(provider, _TAXONOMY_CONTRACT, rule_id)
    if plugin_fail or contract_fail:
        return tuple(list(plugin_fail) + list(contract_fail))

    findings = _line_findings(
        contract,
        _TAXONOMY_CONTRACT,
        rule_id,
        _TAXONOMY_PARALLEL,
        "Behavioral test taxonomy must stay owned by module-level pytestmark",
        respect_exempt=False,
    )
    block_failed = (
        not _present(plugin, 'getattr(module, "pytestmark"')
        or not _present(plugin, '"modules": modules')
        or not _present(plugin, '"nodes": nodes')
        or not _present_re(contract, re.compile(r"^def _assert_marker_only_taxonomy\("))
        or not _present_re(
            contract, re.compile(r"^def test_tm003_multiple_node_classifications_fail\(")
        )
        or not _present_re(
            contract, re.compile(r"^def test_tm003_mixed_module_classifications_fail\(")
        )
        or not _present_re(
            contract, re.compile(r"^def test_tm004_new_module_classification_needs_no_whitelist\(")
        )
        or bool(findings)
    )
    if block_failed and not findings:
        findings.append(
            _summary(
                rule_id,
                _TAXONOMY_CONTRACT,
                "Behavioral test taxonomy must stay owned by module-level pytestmark",
            )
        )
    return tuple(findings)


_IDENTITY_OWNER = "src/apm_cli/models/dependency/identity.py"


_MATERIALIZATION_OWNER = "src/apm_cli/models/dependency/materialization.py"


_REFERENCE_OWNER = "src/apm_cli/models/dependency/reference.py"


_RESOLVE_PHASE = "src/apm_cli/install/phases/resolve.py"


def check_dependency_identity(provider: FactsProvider) -> tuple[Violation, ...]:
    """Identity may casefold only in identity.py; materialization preserves casing."""
    rule_id = _GUARD_DEPENDENCY_IDENTITY
    identity, identity_fail = _facts_for(provider, _IDENTITY_OWNER, rule_id)
    materialization, mat_fail = _facts_for(provider, _MATERIALIZATION_OWNER, rule_id)
    reference, ref_fail = _facts_for(provider, _REFERENCE_OWNER, rule_id)
    resolve, resolve_fail = _facts_for(provider, _RESOLVE_PHASE, rule_id)
    failures = list(identity_fail) + list(mat_fail) + list(ref_fail) + list(resolve_fail)
    if failures:
        return tuple(failures)

    findings: list[Violation] = []
    unique_key_body = _awk_body(
        identity, re.compile(r"^def build_dependency_unique_key\("), re.compile(r"^def ")
    )
    install_path_body = _awk_body(
        reference, re.compile(r"^    def get_install_path\("), re.compile(r"^    def ")
    )
    materialization_body = _awk_body(
        materialization, re.compile(r"^def build_materialization_path\("), re.compile(r"^def ")
    )
    if (
        not _body_has(unique_key_body, "normalize_package_repo_url(")
        or not _present_re(materialization, re.compile(r"^def prepare_materialization_path\("))
        or not _present(resolve, "prepare_materialization_path(")
        or not _body_has(
            install_path_body, "return build_materialization_path(self, apm_modules_dir)"
        )
        or not _body_has(materialization_body, 'repo_parts = dependency.repo_url.split("/")')
        or _body_has_re(
            materialization_body,
            re.compile(r"canonical_repo_url|normalize_package_repo_url|\.lower\(\)|\.casefold\(\)"),
        )
        or _present(reference, "self.repo_url = normalize_package_repo_url")
    ):
        findings.append(
            _summary(
                rule_id,
                _MATERIALIZATION_OWNER,
                "Dependency identity may casefold only in identity.py; "
                "materialization must preserve source casing",
            )
        )
    if not _present(identity, "if is_github_hostname(effective_host):") or _present_re(
        identity, re.compile(r"effective_host.*==.*default_host|configured_default_host")
    ):
        findings.append(
            _summary(
                rule_id,
                _IDENTITY_OWNER,
                "Package identity casing must route through is_github_hostname",
            )
        )
    return tuple(findings)


_POLICY_OWNER = "src/apm_cli/policy/discovery.py"


_POLICY_PREFIX = "src/apm_cli/policy/"


_ADO_COORDINATE_MESSAGE = "ADO policy coordinate must come from discovery.py constants"


_POLICY_SHAPE_MESSAGE = (
    "Cached policy shape must route through policy/discovery.py::_policy_to_dict"
)


_ADO_PROJECT_OWNER = re.compile(r'^ADO_POLICY_PROJECT = "apm"$')


_ADO_REPOSITORY_OWNER = re.compile(r'^ADO_POLICY_REPOSITORY = "apm-policy"$')


_ADO_COORDINATE_CONSUMER = re.compile(
    r"project=ADO_POLICY_PROJECT|ADO_POLICY_PROJECT, ADO_POLICY_REPOSITORY"
)


_ADO_COORDINATE_DUPLICATE = re.compile(r"^[ \t]*ADO_POLICY_(PROJECT|REPOSITORY)[ \t]*=")


_ADO_LITERAL_CONSUMER = re.compile(
    r"""project[ \t]*=[ \t]*["']apm["']|repo[ \t]*=[ \t]*["']apm-policy["']"""
)


_POLICY_NAMED_DEFS = re.compile(
    r"^[ \t]*def [A-Za-z0-9_]*(policy_to_dict|serialize_policy)[A-Za-z0-9_]*\("
)


_POLICY_TO_DICT_CALL = re.compile(r"^[ \t]*[^#]*_policy_to_dict\(policy\)")


_POLICY_SERIALIZED_ASSIGN = re.compile(r"^[ \t]*serialized[ \t]*=[ \t]*_serialize_policy\(policy\)")


_EXPECTED_ADO_OWNER_COUNT = 1


_EXPECTED_ADO_CONSUMER_COUNT = 2


_EXPECTED_POLICY_NAMED_DEFS = 2


def _failed_subconditions(subconditions: Sequence[tuple[str, bool]]) -> tuple[str, ...]:
    """Return the names of the subconditions that evaluated to failed."""
    return tuple(name for name, failed in subconditions if failed)


def _subcondition_summary(rule_id: str, message: str, failed: Sequence[str]) -> Violation:
    """Return one block summary that names the exact failed subconditions."""
    return _summary(rule_id, _POLICY_OWNER, f"{message}; failed: {', '.join(failed)}")


def _ado_coordinate_findings(
    provider: FactsProvider, policy: object, rule_id: str
) -> list[Violation]:
    """Legacy shell L388-416: five ADO policy-coordinate subconditions."""
    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=_ADO_COORDINATE_DUPLICATE,
        owner=_POLICY_OWNER,
        message=f"{_ADO_COORDINATE_MESSAGE}; parallel coordinate definition",
        respect_exempt=True,
    )
    literal_consumers = _line_findings(
        policy,
        _POLICY_OWNER,
        rule_id,
        _ADO_LITERAL_CONSUMER,
        f"{_ADO_COORDINATE_MESSAGE}; literal coordinate bypasses the constants",
        respect_exempt=True,
    )
    failed = _failed_subconditions(
        (
            (
                "project-owner-count",
                _count_re(policy, _ADO_PROJECT_OWNER) != _EXPECTED_ADO_OWNER_COUNT,
            ),
            (
                "repository-owner-count",
                _count_re(policy, _ADO_REPOSITORY_OWNER) != _EXPECTED_ADO_OWNER_COUNT,
            ),
            (
                "consumer-count",
                _count_re(policy, _ADO_COORDINATE_CONSUMER) != _EXPECTED_ADO_CONSUMER_COUNT,
            ),
            ("duplicate-definitions", bool(duplicates)),
            ("literal-consumers", bool(literal_consumers)),
        )
    )
    if not failed:
        return []
    findings = [_subcondition_summary(rule_id, _ADO_COORDINATE_MESSAGE, failed)]
    findings.extend(duplicates)
    findings.extend(literal_consumers)
    return findings


def _cached_policy_shape_findings(
    provider: FactsProvider, policy: object, rule_id: str
) -> list[Violation]:
    """Legacy shell L417-447: four cached-policy-shape subconditions."""
    serializer_body = _awk_body(
        policy, re.compile(r"^def _serialize_policy\("), re.compile(r"^def ")
    )
    cache_write_body = _awk_body(policy, re.compile(r"^def _write_cache\("), re.compile(r"^def "))
    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_POLICY_PREFIX,
        pattern=_POLICY_NAMED_DEFS,
        owner=_POLICY_OWNER,
        message=f"{_POLICY_SHAPE_MESSAGE}; parallel serializer definition",
        respect_exempt=True,
    )
    failed = _failed_subconditions(
        (
            (
                "named-def-count",
                _count_re(policy, _POLICY_NAMED_DEFS) != _EXPECTED_POLICY_NAMED_DEFS,
            ),
            (
                "serializer-body-calls-policy-to-dict",
                not _body_has_re(serializer_body, _POLICY_TO_DICT_CALL),
            ),
            (
                "cache-write-body-assigns-serialized",
                not _body_has_re(cache_write_body, _POLICY_SERIALIZED_ASSIGN),
            ),
            ("duplicate-definitions", bool(duplicates)),
        )
    )
    if not failed:
        return []
    findings = [_subcondition_summary(rule_id, _POLICY_SHAPE_MESSAGE, failed)]
    findings.extend(duplicates)
    return findings


def check_cached_policy_shape(provider: FactsProvider) -> tuple[Violation, ...]:
    """Cached policy shape and ADO policy coordinate must stay in discovery.py."""
    rule_id = _GUARD_CACHED_POLICY
    policy, policy_fail = _facts_for(provider, _POLICY_OWNER, rule_id)
    if policy_fail:
        return tuple(policy_fail)
    return tuple(
        _ado_coordinate_findings(provider, policy, rule_id)
        + _cached_policy_shape_findings(provider, policy, rule_id)
    )


_APPLY_TO_OWNER = "src/apm_cli/utils/patterns.py"


_APPLY_TO_PARSER = "src/apm_cli/primitives/parser.py"


_HIDDEN_TOOL_OWNER = "src/apm_cli/compilation/context_optimizer.py"


def check_apply_to_placement(provider: FactsProvider) -> tuple[Violation, ...]:
    """applyTo parsing uses utils/patterns.py; hidden placement uses ContextOptimizer."""
    rule_id = _GUARD_APPLY_TO
    owner, owner_fail = _facts_for(provider, _APPLY_TO_OWNER, rule_id)
    parser, parser_fail = _facts_for(provider, _APPLY_TO_PARSER, rule_id)
    hidden, hidden_fail = _facts_for(provider, _HIDDEN_TOOL_OWNER, rule_id)
    if owner_fail or parser_fail or hidden_fail:
        return tuple(list(owner_fail) + list(parser_fail) + list(hidden_fail))

    normalizer_defs = _count_defs_across(
        provider, _SRC_PREFIX, re.compile(r"^def _?normalize_apply_to\(")
    )
    prefix_defs = _count_defs_across(
        provider, _SRC_PREFIX, re.compile(r"^def literal_apply_to_top_level_roots\(")
    )
    hidden_tree_defs = _count_defs_across(
        provider, _SRC_PREFIX, re.compile(r"^PLACEMENT_HIDDEN_TOOL_TREES[ \t]*=")
    )
    if (
        normalizer_defs != 1
        or not _present_re(owner, re.compile(r"^def normalize_apply_to\("))
        or prefix_defs != 1
        or not _present_re(owner, re.compile(r"^def literal_apply_to_top_level_roots\("))
        or not _present(parser, "from apm_cli.utils.patterns import normalize_apply_to")
        or _present_re(parser, re.compile(r"^def _?normalize_apply_to\("))
        or not _present(parser, 'normalize_apply_to(metadata.get("applyTo"), default="")')
        or hidden_tree_defs != 1
        or not _present_re(hidden, re.compile(r"^PLACEMENT_HIDDEN_TOOL_TREES = frozenset\("))
        or not _present(hidden, "literal_apply_to_top_level_roots(")
        or _present_re(hidden, re.compile(r"^    def _targeted_top_level_roots\("))
        or not _present(hidden, "not self._is_supported_hidden_tool_root(path)")
    ):
        return (
            _summary(
                rule_id,
                _APPLY_TO_OWNER,
                "applyTo parsing must use utils/patterns.py and hidden placement ContextOptimizer",
            ),
        )
    return ()


_FRONTMATTER_OWNER = "src/apm_cli/utils/yaml_io.py"


def check_frontmatter_yaml(provider: FactsProvider) -> tuple[Violation, ...]:
    """Frontmatter BOM decoding must route through utils/yaml_io.py."""
    rule_id = _GUARD_FRONTMATTER
    owner, owner_fail = _facts_for(provider, _FRONTMATTER_OWNER, rule_id)
    if owner_fail:
        return tuple(owner_fail)

    duplicates = _duplicate_definition_lines(
        provider,
        rule_id=rule_id,
        prefix=_SRC_PREFIX,
        pattern=re.compile(r"utf-8-sig"),
        owner=_FRONTMATTER_OWNER,
        message="Frontmatter BOM decoding must route through utils/yaml_io.py",
        respect_exempt=True,
    )
    findings = list(duplicates)
    if not _present(
        owner, 'def load_frontmatter(fd: Any, encoding: str = "utf-8-sig")'
    ) or not _present(owner, 'text.removeprefix("\\ufeff")'):
        findings.append(
            _summary(
                rule_id,
                _FRONTMATTER_OWNER,
                "Frontmatter BOM decoding must route through utils/yaml_io.py",
            )
        )
    return tuple(findings)


_LIFECYCLE_CONTRACT = "tests/quality/test_ci_topology.py"


def check_lifecycle_partition(provider: FactsProvider) -> tuple[Violation, ...]:
    """Lifecycle marker partitions must be collection-derived, never pinned."""
    rule_id = "contracts-tests-lifecycle-smoke-partition"
    contract, contract_fail = _facts_for(provider, _LIFECYCLE_CONTRACT, rule_id)
    if contract_fail:
        return tuple(contract_fail)

    findings = _line_findings(
        contract,
        _LIFECYCLE_CONTRACT,
        rule_id,
        re.compile(
            r"LIFECYCLE_SMOKE_(FULL_COUNT|MERGE_GROUP_COUNT|REQUIRED_COUNT|MERGE_GROUP_NODES)"
            r"|expected_(full_count|merge_group_nodes|required_count)"
        ),
        "Lifecycle marker partitions must be collection-derived, never count/list pinned",
        respect_exempt=False,
    )
    block_failed = (
        not _present_re(contract, re.compile(r"^def _validated_lifecycle_node_set\("))
        or not _present_re(contract, re.compile(r"^def _assert_lifecycle_partition_sets\("))
        or not _present(contract, "merge_group < full")
        or not _present(contract, "required == full - merge_group")
        or bool(findings)
    )
    if block_failed and not findings:
        findings.append(
            _summary(
                rule_id,
                _LIFECYCLE_CONTRACT,
                "Lifecycle marker partitions must be collection-derived, never count/list pinned",
            )
        )
    return tuple(findings)


_CONTRACT_RULE_ID = "contracts-tests-executable-contract-authorities"


def check_test_contract_authorities(provider: FactsProvider) -> tuple[Violation, ...]:
    """Executable test contract authorities (binary, parity, ratchet)."""
    rule_id = _CONTRACT_RULE_ID
    findings: list[Violation] = []
    findings.extend(find_binary_selection_violations(provider, rule_id))
    findings.extend(find_rendered_parity_violations(provider, rule_id))
    findings.extend(find_ratchet_authority_violations(provider, rule_id))
    return tuple(findings)


def _owner_rule(guard_id: str, description: str, check) -> Rule:
    """Build one owner rule whose id and single guard id are the guard id."""
    return Rule(
        id=guard_id,
        group=GROUP,
        guard_ids=(guard_id,),
        description=description,
        check=check,
    )


def _structural_rule(rule_id: str, description: str, check) -> Rule:
    """Build one guard-less structural rule (real check, no owner guard)."""
    return Rule(
        id=rule_id,
        group=GROUP,
        guard_ids=(),
        description=description,
        check=check,
    )


RULES: tuple[Rule, ...] = (
    _owner_rule(
        _GUARD_TAXONOMY,
        "Behavioral test taxonomy classification stays owned by module-level pytestmark.",
        check_taxonomy_classification,
    ),
    _owner_rule(
        _GUARD_DEPENDENCY_IDENTITY,
        "Dependency comparison identity casefolds only in identity.py; materialization preserves casing.",
        check_dependency_identity,
    ),
    _owner_rule(
        _GUARD_CACHED_POLICY,
        "Cached policy shape and ADO coordinate stay owned by policy/discovery.py.",
        check_cached_policy_shape,
    ),
    _owner_rule(
        _GUARD_APPLY_TO,
        "applyTo normalization and hidden-tool placement stay owned by their canonical modules.",
        check_apply_to_placement,
    ),
    _owner_rule(
        _GUARD_FRONTMATTER,
        "Frontmatter BOM decoding and bounded YAML parsing stay owned by utils/yaml_io.py.",
        check_frontmatter_yaml,
    ),
    _owner_rule(
        _GUARD_LOCKFILE_READ,
        "Read-only lockfile path resolution stays owned by deps/lockfile.py.",
        check_lockfile_read_resolution,
    ),
    _owner_rule(
        _GUARD_GENERATION_FOOTER,
        "Generated-content footer wording stays owned by compilation/footer.py.",
        lambda provider: check_generation_footer_authority(provider, _GUARD_GENERATION_FOOTER),
    ),
    _structural_rule(
        _CONTRACT_RULE_ID,
        "Executable test binary selection, rendered CLI parity, and ratchet authority owners.",
        check_test_contract_authorities,
    ),
    _structural_rule(
        "contracts-tests-lifecycle-smoke-partition",
        "Lifecycle smoke marker partitions are collection-derived, never count/list pinned.",
        check_lifecycle_partition,
    ),
    *(_structural_rule(entry.rule_id, entry.description, entry.check) for entry in LEGACY_CHECKS),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
