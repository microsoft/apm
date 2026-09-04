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


_GUARD_PROJECT_YAML_WRITES = "contracts-tooling-project-yaml-write-delegation"


_GUARD_LOCKFILE_READ = "contracts-tooling-lockfile-read"


_GUARD_LOCKFILE_TIMESTAMP = "contracts-tooling-lockfile-timestamp"


_GUARD_LOCKFILE_TIMESTAMP_FALLBACK = "contracts-tooling-lockfile-timestamp-fallback"


_GUARD_LOCKFILE_TIMESTAMP_CONSTRUCTOR = "contracts-tooling-lockfile-timestamp-constructor"


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


def _assigns_generated_at(target: ast.expr) -> bool:
    """Return whether an assignment target writes lockfile timestamp metadata."""
    if isinstance(target, ast.Attribute):
        return target.attr == "generated_at"
    if isinstance(target, (ast.List, ast.Tuple)):
        return any(_assigns_generated_at(item) for item in target.elts)
    return False


def check_lockfile_timestamp_authority(provider: FactsProvider) -> tuple[Violation, ...]:
    """Lockfile timestamp writes must stay inside the lockfile owner."""
    rule_id = _GUARD_LOCKFILE_TIMESTAMP
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _LOCKFILE_OWNER:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures or facts.tree_index is None:
            continue
        for node in facts.tree_index.nodes:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = (node.target,)
            else:
                continue
            if any(_assigns_generated_at(target) for target in targets):
                findings.append(
                    violation(
                        rule_id,
                        path,
                        "Lockfile timestamp writes and fallback policy must route through "
                        "deps/lockfile.py",
                        line=node.lineno,
                    )
                )
    return tuple(findings)


def _constructs_lockfile_timestamp(node: ast.AST) -> bool:
    """Return whether a LockFile constructor sets timestamp metadata."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        is_lockfile = node.func.id == "LockFile"
    else:
        is_lockfile = isinstance(node.func, ast.Attribute) and node.func.attr == "LockFile"
    return is_lockfile and any(keyword.arg == "generated_at" for keyword in node.keywords)


def check_lockfile_timestamp_constructor(provider: FactsProvider) -> tuple[Violation, ...]:
    """Lockfile timestamp construction must stay inside its owner."""
    rule_id = _GUARD_LOCKFILE_TIMESTAMP_CONSTRUCTOR
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _LOCKFILE_OWNER:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures or facts.tree_index is None:
            continue
        findings.extend(
            violation(
                rule_id,
                path,
                "Lockfile timestamp writes and fallback policy must route through deps/lockfile.py",
                line=node.lineno,
            )
            for node in facts.tree_index.nodes
            if _constructs_lockfile_timestamp(node)
        )
    return tuple(findings)


def _owns_reproducible_fallback(node: ast.AST) -> bool:
    """Return whether a node reimplements the reproducible timestamp fallback."""
    if isinstance(node, ast.Constant):
        return node.value == "1970-01-01T00:00:00+00:00"
    if isinstance(node, ast.Call) and node.args:
        first_arg = node.args[0]
        return (
            isinstance(first_arg, ast.Constant)
            and first_arg.value == "SOURCE_DATE_EPOCH"
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "getenv"}
        )
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "SOURCE_DATE_EPOCH"
    return False


def check_lockfile_timestamp_fallback(provider: FactsProvider) -> tuple[Violation, ...]:
    """Reproducible timestamp fallback policy must stay inside its owner."""
    rule_id = _GUARD_LOCKFILE_TIMESTAMP_FALLBACK
    findings: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _LOCKFILE_OWNER:
            continue
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures or facts.tree_index is None:
            continue
        findings.extend(
            violation(
                rule_id,
                path,
                "Lockfile timestamp writes and fallback policy must route through deps/lockfile.py",
                line=node.lineno,
            )
            for node in facts.tree_index.nodes
            if _owns_reproducible_fallback(node)
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
    """Guard dependency identity, materialization, and embedded-subpath ownership."""
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
    embedded_subpath_body = _awk_body(
        reference,
        re.compile(r"^    def _check_no_embedded_subpath\("),
        re.compile(r"^    def "),
    )
    embedded_subpath_message = (
        "Embedded git URL subpath validation must use DependencyReference and host_providers"
    )
    if not _body_has(embedded_subpath_body, "classify_host_provider(") or not _body_has(
        embedded_subpath_body, 'provider.kind == "gitlab"'
    ):
        findings.append(
            _summary(
                rule_id,
                _REFERENCE_OWNER,
                embedded_subpath_message,
            )
        )
    primitive_dirs = re.compile(r"_APM_PRIMITIVE_DIRS")
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _REFERENCE_OWNER:
            continue
        facts, path_failures = _facts_for(provider, path, rule_id)
        findings.extend(path_failures)
        if path_failures:
            continue
        findings.extend(
            _line_findings(
                facts,
                path,
                rule_id,
                primitive_dirs,
                embedded_subpath_message,
                respect_exempt=True,
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
_INSTRUCTION_INTEGRATOR = "src/apm_cli/integration/instruction_integrator.py"
_CONTENT_SCANNER = "src/apm_cli/security/content_scanner.py"
_INSTALL_SERVICES = "src/apm_cli/install/services.py"
_REVISION_PINS = "src/apm_cli/deps/revision_pins.py"
_FRONTMATTER_METHODS = frozenset({"load", "loads", "parse"})


def _frontmatter_aliases(nodes: Sequence[ast.AST]) -> tuple[set[str], dict[str, str]]:
    """Return module aliases and imported parser-function aliases."""
    modules: set[str] = set()
    functions: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "frontmatter":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "frontmatter":
            for alias in node.names:
                if alias.name in _FRONTMATTER_METHODS:
                    functions[alias.asname or alias.name] = alias.name
    return modules, functions


def _frontmatter_call_name(
    node: ast.Call,
    modules: set[str],
    functions: dict[str, str],
) -> str | None:
    """Return the frontmatter parser entry point called by *node*."""
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in modules
        and node.func.attr in _FRONTMATTER_METHODS
    ):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return functions.get(node.func.id)
    return None


def _is_bounded_detect(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_BOUNDED_FRONTMATTER_HANDLER"
        and node.func.attr == "detect"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "text"
    )


def _is_bounded_loads(node: ast.Call, modules: set[str], functions: dict[str, str]) -> bool:
    if _frontmatter_call_name(node, modules, functions) != "loads":
        return False
    handler = next((item.value for item in node.keywords if item.arg == "handler"), None)
    return (
        len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "text"
        and isinstance(handler, ast.Name)
        and handler.id == "_BOUNDED_FRONTMATTER_HANDLER"
    )


def _manual_frontmatter_detector(node: ast.Call) -> bool:
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
        and node.func.attr in {"compile", "match"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("^---")
    ):
        return True
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"startswith", "split"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("---")
    )


def _self_method_call(node: ast.Call, method: str) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == method
    )


def _inside_matching_if(tree, node: ast.AST, predicate) -> bool:
    """Return whether *node* is nested in an if whose test matches."""
    parent = tree.parent(node)
    while parent is not None:
        if isinstance(parent, ast.If) and predicate(parent.test):
            return True
        parent = tree.parent(parent)
    return False


def _is_no_targets_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.Not)
        and isinstance(node.operand, ast.Name)
        and node.operand.id == "targets"
    )


def _is_native_plugin_test(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "admits_native_plugin"
    )


def _prepared_identity_content(node: ast.AST) -> bool:
    candidate = node.value if isinstance(node, ast.Attribute) and node.attr == "content" else node
    return (
        isinstance(candidate, ast.Subscript)
        and isinstance(candidate.value, ast.Name)
        and candidate.value.id == "prepared_instructions"
        and isinstance(candidate.slice, ast.Name)
        and candidate.slice.id == "source_file"
    )


def check_frontmatter_yaml(provider: FactsProvider) -> tuple[Violation, ...]:
    """Frontmatter detection, BOM decoding, and parsing must use yaml_io.py."""
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
    scanner, scanner_fail = _facts_for(provider, _CONTENT_SCANNER, rule_id)
    findings.extend(scanner_fail)
    if not scanner_fail and (
        not _present(scanner, "content = _combine_surrogate_pairs(content)")
        or not _present(scanner, "0xD800,")
        or not _present(scanner, "0xDFFF,")
    ):
        findings.append(
            _summary(
                rule_id,
                _CONTENT_SCANNER,
                "decoded frontmatter scanning must normalize and reject UTF-16 surrogates",
            )
        )
    services, services_fail = _facts_for(provider, _INSTALL_SERVICES, rule_id)
    findings.extend(services_fail)
    if not services_fail and services.tree_index is not None:
        service_tree = services.tree_index
        integration = service_tree.function("integrate_package_primitives")
        service_scope = service_tree.own_scope(integration) if integration is not None else ()
        preflight_calls = [
            node
            for node in service_scope
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "preflight_instructions_for_targets"
        ]
        reconcile_calls = [
            node
            for node in service_scope
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_reconcile_excluded_targets"
        ]
        no_target_calls = [
            node
            for node in reconcile_calls
            if _inside_matching_if(service_tree, node, _is_no_targets_test)
        ]
        native_calls = [
            node
            for node in reconcile_calls
            if _inside_matching_if(service_tree, node, _is_native_plugin_test)
        ]
        post_preflight_calls = [
            node
            for node in reconcile_calls
            if node not in no_target_calls and node not in native_calls
        ]
        if (
            len(preflight_calls) != 1
            or len(no_target_calls) != 1
            or len(native_calls) != 1
            or len(post_preflight_calls) != 1
            or post_preflight_calls[0].lineno <= preflight_calls[0].lineno
        ):
            findings.append(
                _summary(
                    rule_id,
                    _INSTALL_SERVICES,
                    "instruction preflight must precede non-empty target reconciliation",
                )
            )

    tree = owner.tree_index
    loads_function = tree.function("loads_frontmatter") if tree is not None else None
    load_function = tree.function("load_frontmatter") if tree is not None else None
    if tree is None or loads_function is None or load_function is None:
        findings.append(
            _summary(
                rule_id,
                _FRONTMATTER_OWNER,
                "Frontmatter parsing must expose load_frontmatter and loads_frontmatter",
            )
        )
    else:
        modules, functions = _frontmatter_aliases(tree.nodes)
        loads_scope = tree.own_scope(loads_function)
        parser_calls = [
            node
            for node in loads_scope
            if isinstance(node, ast.Call)
            and _frontmatter_call_name(node, modules, functions) is not None
        ]
        detect_calls = [
            node for node in loads_scope if isinstance(node, ast.Call) and _is_bounded_detect(node)
        ]
        bounded_calls = [
            node for node in parser_calls if _is_bounded_loads(node, modules, functions)
        ]
        load_delegates = [
            node
            for node in tree.own_scope(load_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "loads_frontmatter"
        ]
        if len(detect_calls) != 1 or len(parser_calls) != 1 or len(bounded_calls) != 1:
            findings.append(
                _summary(
                    rule_id,
                    _FRONTMATTER_OWNER,
                    "loads_frontmatter must gate exactly one bounded frontmatter.loads call",
                )
            )
        if len(load_delegates) != 1:
            findings.append(
                _summary(
                    rule_id,
                    _FRONTMATTER_OWNER,
                    "load_frontmatter must delegate parsed text to loads_frontmatter",
                )
            )

    for path in _python_paths(provider, _SRC_PREFIX):
        facts, facts_fail = _facts_for(provider, path, rule_id)
        findings.extend(facts_fail)
        if facts_fail or facts.tree_index is None:
            continue
        modules, functions = _frontmatter_aliases(facts.tree_index.nodes)
        for node in facts.tree_index.nodes:
            if not isinstance(node, ast.Call):
                continue
            parser_name = _frontmatter_call_name(node, modules, functions)
            if path != _FRONTMATTER_OWNER and parser_name in _FRONTMATTER_METHODS:
                findings.append(
                    violation(
                        rule_id,
                        path,
                        "direct frontmatter parsing must route through utils/yaml_io.py",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
            if path == _INSTRUCTION_INTEGRATOR and _manual_frontmatter_detector(node):
                findings.append(
                    violation(
                        rule_id,
                        path,
                        "instruction frontmatter detection must route through loads_frontmatter",
                        line=node.lineno,
                        column=node.col_offset + 1,
                    )
                )
        if path == _INSTRUCTION_INTEGRATOR:
            integrate_function = facts.tree_index.function(
                "InstructionIntegrator.integrate_instructions_for_target"
            )
            integrate_scope = (
                facts.tree_index.own_scope(integrate_function)
                if integrate_function is not None
                else ()
            )
            prepare_calls = [
                node
                for node in integrate_scope
                if isinstance(node, ast.Call) and _self_method_call(node, "_prepare_instruction")
            ]
            identity_renders = [
                node
                for node in integrate_scope
                if isinstance(node, ast.Call) and _self_method_call(node, "_render_instruction")
            ]
            adoption_calls = [
                node
                for node in integrate_scope
                if isinstance(node, ast.Call) and _self_method_call(node, "_check_adopt_or_skip")
            ]
            expected_content = (
                next(
                    (
                        item.value
                        for item in adoption_calls[0].keywords
                        if item.arg == "expected_content"
                    ),
                    None,
                )
                if len(adoption_calls) == 1
                else None
            )
            prepared_value = (
                next(
                    (item.value for item in identity_renders[0].keywords if item.arg == "prepared"),
                    None,
                )
                if len(identity_renders) == 1
                else None
            )
            if (
                len(prepare_calls) != 1
                or len(identity_renders) != 1
                or not _prepared_identity_content(prepared_value)
                or not isinstance(expected_content, ast.Name)
                or expected_content.id != "new_content"
            ):
                findings.append(
                    _summary(
                        rule_id,
                        path,
                        "identity instructions must materialize the prepared canonical parse",
                    )
                )
            prepare_function = facts.tree_index.function(
                "InstructionIntegrator._prepare_instruction"
            )
            prepare_scope = (
                facts.tree_index.own_scope(prepare_function) if prepare_function is not None else ()
            )
            security_calls = [
                node
                for node in prepare_scope
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "SecurityGate"
                and node.func.attr == "scan_text"
            ]
            json_calls = [
                node
                for node in prepare_scope
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "json"
                and node.func.attr == "dumps"
            ]
            force_value = (
                next(
                    (item.value for item in security_calls[0].keywords if item.arg == "force"),
                    None,
                )
                if len(security_calls) == 1
                else None
            )
            ensure_ascii = (
                next(
                    (item.value for item in json_calls[0].keywords if item.arg == "ensure_ascii"),
                    None,
                )
                if len(json_calls) == 1
                else None
            )
            block_checks = [
                node
                for node in prepare_scope
                if isinstance(node, ast.If)
                and isinstance(node.test, ast.Attribute)
                and isinstance(node.test.value, ast.Name)
                and node.test.value.id == "verdict"
                and node.test.attr == "should_block"
            ]
            if (
                len(security_calls) != 1
                or not isinstance(force_value, ast.Name)
                or force_value.id != "force"
                or len(json_calls) != 1
                or not isinstance(ensure_ascii, ast.Constant)
                or ensure_ascii.value is not False
                or len(block_checks) != 1
            ):
                findings.append(
                    _summary(
                        rule_id,
                        path,
                        "decoded frontmatter metadata must cross SecurityGate with force policy",
                    )
                )
    return tuple(findings)


def check_project_yaml_write_delegation(provider: FactsProvider) -> tuple[Violation, ...]:
    """Project YAML writers must delegate to the canonical text-write owner."""
    rule_id = _GUARD_PROJECT_YAML_WRITES
    contracts = (
        (_FRONTMATTER_OWNER, "dump_yaml_roundtrip", "write_text_lf"),
        (_FRONTMATTER_OWNER, "write_yaml_text_atomic", "atomic_write_text"),
        (_REVISION_PINS, "apply_revision_pin_updates", "write_yaml_text_atomic"),
    )
    findings: list[Violation] = []
    for path, function_name, delegate in contracts:
        facts, failures = _facts_for(provider, path, rule_id)
        findings.extend(failures)
        if failures or facts.tree_index is None:
            continue
        function = facts.tree_index.function(function_name)
        if function is None:
            findings.append(_summary(rule_id, path, f"missing project YAML writer {function_name}"))
            continue
        calls = _named_calls(facts.tree_index.own_scope(function), delegate)
        if len(calls) != 1:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"{function_name} must call {delegate} exactly once",
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
        "Dependency identity, materialization, and embedded git URL subpaths have canonical owners.",
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
        "Frontmatter delimiter detection, BOM decoding, and bounded YAML parsing stay owned by utils/yaml_io.py.",
        check_frontmatter_yaml,
    ),
    _owner_rule(
        _GUARD_PROJECT_YAML_WRITES,
        "Project YAML writes route through the canonical deterministic text writers.",
        check_project_yaml_write_delegation,
    ),
    _owner_rule(
        _GUARD_LOCKFILE_READ,
        "Read-only lockfile path resolution stays owned by deps/lockfile.py.",
        check_lockfile_read_resolution,
    ),
    _owner_rule(
        _GUARD_LOCKFILE_TIMESTAMP,
        "Lockfile timestamp emission stays owned by deps/lockfile.py.",
        check_lockfile_timestamp_authority,
    ),
    _owner_rule(
        _GUARD_LOCKFILE_TIMESTAMP_CONSTRUCTOR,
        "Lockfile timestamp construction stays owned by deps/lockfile.py.",
        check_lockfile_timestamp_constructor,
    ),
    _owner_rule(
        _GUARD_LOCKFILE_TIMESTAMP_FALLBACK,
        "Reproducible timestamp fallback stays owned by deps/lockfile.py.",
        check_lockfile_timestamp_fallback,
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
