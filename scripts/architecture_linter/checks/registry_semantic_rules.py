"""Guard-less semantic registry/delegation analyzers.

These rules run as ordinary rules with an empty ``guard_ids`` tuple -- the
owner-registry guard allocation, and the runner's "every registered guard
executes exactly once" invariant, belong entirely to the six owner-guarded
rules in :mod:`registry_owner_guards`. This module only adds legacy
core-runtime units that never had an owner-registry guard allocated:

=========================================  ===============================
Guard-less semantic rule                   Legacy shell provenance
=========================================  ===============================
host-backend-dispatch                      AC1 core/host_providers.py
native-locator-target-names                AC1 copilot-app / copilot-cowork
experimental-target-hints                  AC1 install/target_hints.py
compile-inventory-authority                AC2 check_compile_inventory_...
agents-source-attribution                  AC2 check_agents_source_attri...
lockfile-version-authority                 AC2 deps/lockfile.py
command-machine-output                     AC5 set_console_stderr
logger-redaction-attachment                AC5 apm_logger.addFilter
root-cli-output-mode                       AC5 detect_output_mode pair
policy-ref-redaction                       AC5 _redact_policy_ref pair
manifest-schema-negotiation                AC6 $schema confinement
lifecycle-docs-aggregate                   AC6 lifecycle.md statement
diagnostic-ascii-owner                     AC12 check_diagnostic_ascii_...
=========================================  ===============================

Exemption behavior is preserved per check: a ported guard honors
``architecture-authority-exempt:`` markers only where its legacy
counterpart piped its hits through the exemption filter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from scripts.architecture_linter.checks import registry_legacy
from scripts.architecture_linter.checks.registry_shared import (
    _SRC,
    GROUP,
    _count_regex_lines,
    _has_regex,
    _python_paths,
    _read_required,
)
from scripts.architecture_linter.checks.tree_index import TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    line_pattern_violations,
    require_text,
    source_text,
    violation,
)
from scripts.architecture_linter.models import Rule, Violation

_HOST_BACKEND_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/core/auth.py",
    "src/apm_cli/deps/host_backends.py",
    "src/apm_cli/models/dependency/reference.py",
)


_HOST_BACKEND_PATTERN = r"_BACKEND_BY_KIND|only supports .gitlab.|Supported values: gitlab"


_NATIVE_LOCATOR_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/install/deployed_paths.py",
    "src/apm_cli/install/manifest_reconcile.py",
)


_NATIVE_LOCATOR_PATTERN = r'name == "copilot-(app|cowork)"|name in \{.*copilot-(app|cowork)'


_EXPERIMENTAL_HINT_OWNER = "src/apm_cli/install/target_hints.py"


_EXPERIMENTAL_HINT_DEF_PATTERN = r"^def emit_disabled_experimental_target_hint\("


_EXPERIMENTAL_HINT_TEXT = "requires an experimental flag"


_COMPILE_INVENTORY_OWNER = "src/apm_cli/compilation/inventory.py"


_COMPILE_OPTIMIZER = "src/apm_cli/compilation/context_optimizer.py"


_COMPILE_DISCOVERY = "src/apm_cli/primitives/discovery.py"


_COMPILE_AGENTS = "src/apm_cli/compilation/agents_compiler.py"


_COMPILE_INVENTORY_CLASS = "class CompileInventory"


_OS_WALK = "os.walk("


_COMPILE_OWNER_FRAGMENTS = (
    'if path != root and (".git" in file_names or ".git" in child_dirs):',
    "nested_repository_roots.add(path)",
    "def nested_repository_root_for(",
)


_COMPILE_REQUIRED_FRAGMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        _COMPILE_OPTIMIZER,
        (
            "from .inventory import CompileInventory",
            "inventory = self._inventory or CompileInventory.collect(self.base_dir)",
            "inventory.files_under(self._scan_top_level_roots)",
        ),
    ),
    (
        _COMPILE_DISCOVERY,
        (
            "inventory: CompileInventory | None = None",
            "inventory = CompileInventory.collect(base_path, exclude_patterns=exclude_patterns)",
            "inventory.files_within(base_path)",
            "inventory.nested_repository_root_for(directory)",
        ),
    ),
    (
        registry_legacy.DISTRIBUTED_COMPILER_PATH,
        (
            "source_inventory: CompileInventory | None = None",
            "deploy_inventory: CompileInventory | None = None",
            "deploy_inventory.nested_repository_root_for(directory_path)",
            "for directory_path, (relative_path, files) in sorted(cleanup_directories.items()):",
        ),
    ),
    (
        _COMPILE_AGENTS,
        (
            "self._source_inventory = CompileInventory.collect(",
            "self.source_dir, exclude_patterns=config.exclude",
            "source_inventory=self._source_inventory",
            "deploy_inventory=self._deploy_inventory",
            "deploy_inventory.nested_repository_root_for(agents_path.parent)",
        ),
    ),
)


_COMPILE_FORBIDDEN_FRAGMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_COMPILE_AGENTS, ("_nested_git_repository_root", ' / ".git"')),
    (registry_legacy.DISTRIBUTED_COMPILER_PATH, (' / ".git"',)),
    (_COMPILE_DISCOVERY, (' / ".git"',)),
)


_COMPILE_INVENTORY_PATHS: tuple[str, ...] = (
    _COMPILE_INVENTORY_OWNER,
    _COMPILE_OPTIMIZER,
    _COMPILE_DISCOVERY,
    registry_legacy.DISTRIBUTED_COMPILER_PATH,
    _COMPILE_AGENTS,
)

_ROOT_CONTEXT_OWNER = "src/apm_cli/compilation/agents_compiler.py"

_ROOT_CONTEXT_CLI = "src/apm_cli/commands/compile/cli.py"

_ROOT_CONTEXT_PATHS = (_ROOT_CONTEXT_OWNER, _ROOT_CONTEXT_CLI)

_ROOT_CONTEXT_OWNER_FRAGMENTS = (
    "def _hand_authored_root_context_blocks_write(",
    "and self._hand_authored_root_context_blocks_write(root_claude_path)",
    "and self._hand_authored_root_context_blocks_write(output_file)",
    "and self._hand_authored_root_context_blocks_write(agents_path)",
)

_ROOT_CONTEXT_CLI_FRAGMENTS = (
    'intermediate_result.stats.get("agents_root_context_write_blocked", 0)',
    "if not dry_run and not agents_write_blocked:",
)

_ROOT_CONTEXT_DUPLICATE_PATTERN = (
    r"^\s*def\s+\w*(?:hand_authored.*root_context|root_context.*hand_authored"
    r"|(?:replace|overwrite).*root|root.*(?:replace|overwrite))"
    r"\w*\s*\("
)


_LOCKFILE_OWNER = "src/apm_cli/deps/lockfile.py"


_LOCKFILE_VERSION_PATTERN = r"SUPPORTED_LOCKFILE_VERSIONS|lockfile_version\s+(==|!=|in)"


_COMMANDS_PREFIX = "src/apm_cli/commands/"


_CONSOLE_STDERR_PATTERN = r"set_console_stderr"


_ROOT_CLI = "src/apm_cli/cli.py"


_PACKAGE_LOGGER_FILTER_PATTERN = r'apm_logger\.addFilter|logging\.getLogger\("apm_cli"\)\.addFilter'


_ROOT_CLI_REQUIRED_PATTERNS: tuple[str, ...] = ("detect_output_mode", "handler.addFilter")


_POLICY_DISCOVERY = "src/apm_cli/policy/discovery.py"


_POLICY_REDACTION_REQUIRED: tuple[str, ...] = (
    '"repo_ref": _redact_policy_ref(repo_ref)',
    '"chain_refs": [_redact_policy_ref(ref) for ref in persisted_chain_refs]',
)


_MANIFEST_CONTRACT_OWNER = "src/apm_cli/models/manifest_contract.py"


_MANIFEST_SCHEMA_OWNERS = (
    _MANIFEST_CONTRACT_OWNER,
    "src/apm_cli/agent_plugins/loader.py",
    "src/apm_cli/agent_plugins/validation.py",
)


_MANIFEST_SCHEMA_PATTERN = r'get\(["\']\$schema["\']\)'


_LIFECYCLE_DOC = "docs/src/content/docs/concepts/lifecycle.md"


_LIFECYCLE_DOC_REQUIRED = "does not run aggregate"


def _as_violations(
    rule_id: str, findings: Sequence[registry_legacy.LegacyFinding]
) -> tuple[Violation, ...]:
    """Adapt structural analyzer findings into engine violations."""
    return tuple(
        violation(rule_id, finding.path, finding.message, line=finding.line) for finding in findings
    )


def _check_host_backend_dispatch(provider: FactsProvider) -> Iterable[Violation]:
    """Host backend dispatch must come from ``core/host_providers.py``.

    Ports the AC1 ``check_pattern`` forbidding a parallel backend table
    (``_BACKEND_BY_KIND``) or hard-coded GitLab-only vocabulary in the auth
    resolver, the host-backend module, and the dependency reference model.
    """
    rule_id = "registry_delegation.host_backend_dispatch"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_HOST_BACKEND_CONSUMERS,
        pattern=_HOST_BACKEND_PATTERN,
        message="host backend dispatch must come from core/host_providers.py",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_native_locator_target_names(provider: FactsProvider) -> Iterable[Violation]:
    """Install orchestration must not branch on native locator target names.

    Ports the AC1 ``check_pattern`` forbidding ``name == "copilot-app"`` style
    literal comparisons (and their set-membership form) in the deployed-path
    and manifest-reconcile modules.
    """
    rule_id = "registry_delegation.native_locator_target_names"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_NATIVE_LOCATOR_CONSUMERS,
        pattern=_NATIVE_LOCATOR_PATTERN,
        message="install orchestration must not branch on native locator target names",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_experimental_target_hints(provider: FactsProvider) -> Iterable[Violation]:
    """Experimental target hints must route through ``install/target_hints.py``.

    Ports the AC1 composite guard: the owner defines
    ``emit_disabled_experimental_target_hint`` exactly once, and the hint text
    appears nowhere else under ``src/apm_cli``. The legacy duplicate scan piped
    its hits straight to the guard without an exemption filter, so this rule
    deliberately does not honor ``architecture-authority-exempt:`` markers on
    the duplicate leg either.
    """
    rule_id = "registry_delegation.experimental_target_hints"
    owner, failures = checked_facts(
        provider, _EXPERIMENTAL_HINT_OWNER, rule_id, require_python=True
    )
    if failures:
        return failures

    findings: list[Violation] = []
    if _count_regex_lines(owner, _EXPERIMENTAL_HINT_DEF_PATTERN) != 1:
        findings.append(
            violation(
                rule_id,
                _EXPERIMENTAL_HINT_OWNER,
                "emit_disabled_experimental_target_hint must be defined exactly once "
                "by install/target_hints.py",
            )
        )
    findings.extend(
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_EXPERIMENTAL_HINT_OWNER,)),
            pattern=re.escape(_EXPERIMENTAL_HINT_TEXT),
            message="experimental target hints must route through install/target_hints.py",
            exempt_marker=None,
        )
    )
    return findings


def _check_compile_inventory_authority(provider: FactsProvider) -> Iterable[Violation]:
    """Compile nested Git boundaries must route through ``compilation/inventory.py``.

    Ports ``scripts/check_compile_inventory_authority.py`` in full: the single
    ``CompileInventory`` class, the single ``os.walk(`` traversal owned by the
    inventory, consumers that must not walk or probe nested repositories
    themselves, and the wiring fragments the optimizer, primitive discovery,
    distributed compiler, and AGENTS compiler must each carry. The retired
    helper printed exactly one message for any combination of defects, so this
    rule reports exactly one violation and names every failing subcondition.
    """
    rule_id = "registry_delegation.compile_inventory_authority"
    facts_by_path, failures = _read_required(provider, rule_id, _COMPILE_INVENTORY_PATHS)
    if failures:
        return failures

    texts = {path: source_text(facts) for path, facts in facts_by_path.items()}
    defects: list[str] = []
    owner_text = texts[_COMPILE_INVENTORY_OWNER]
    if owner_text.count(_COMPILE_INVENTORY_CLASS) != 1:
        defects.append(
            f"{_COMPILE_INVENTORY_OWNER} must declare {_COMPILE_INVENTORY_CLASS} exactly once"
        )
    if owner_text.count(_OS_WALK) != 1:
        defects.append(f"{_COMPILE_INVENTORY_OWNER} must own exactly one {_OS_WALK} traversal")
    missing_owner = tuple(
        fragment for fragment in _COMPILE_OWNER_FRAGMENTS if fragment not in owner_text
    )
    if missing_owner:
        joined = ", ".join(repr(fragment) for fragment in missing_owner)
        defects.append(f"{_COMPILE_INVENTORY_OWNER} is missing: {joined}")
    for path in (
        _COMPILE_OPTIMIZER,
        registry_legacy.DISTRIBUTED_COMPILER_PATH,
        _COMPILE_DISCOVERY,
    ):
        if _OS_WALK in texts[path]:
            defects.append(f"{path} must not call {_OS_WALK} directly")
    for path, fragments in _COMPILE_REQUIRED_FRAGMENTS:
        missing = tuple(fragment for fragment in fragments if fragment not in texts[path])
        if missing:
            joined = ", ".join(repr(fragment) for fragment in missing)
            defects.append(f"{path} is missing: {joined}")
    for path, fragments in _COMPILE_FORBIDDEN_FRAGMENTS:
        present = tuple(fragment for fragment in fragments if fragment in texts[path])
        if present:
            joined = ", ".join(repr(fragment) for fragment in present)
            defects.append(f"{path} must not contain: {joined}")
    if not defects:
        return ()
    return (
        violation(
            rule_id,
            _COMPILE_INVENTORY_OWNER,
            "compile nested Git boundaries must route through compilation/inventory.py; "
            + "; ".join(defects),
        ),
    )


def _check_root_context_write_eligibility(provider: FactsProvider) -> Iterable[Violation]:
    """Project root overwrite decisions must route through the compiler owner."""
    rule_id = "contracts-tooling-root-context-write-eligibility"
    facts_by_path, failures = _read_required(provider, rule_id, _ROOT_CONTEXT_PATHS)
    if failures:
        return failures

    owner_text = source_text(facts_by_path[_ROOT_CONTEXT_OWNER])
    cli_text = source_text(facts_by_path[_ROOT_CONTEXT_CLI])
    defects: list[str] = []
    if owner_text.count("def _hand_authored_root_context_blocks_write(") != 1:
        defects.append(f"{_ROOT_CONTEXT_OWNER} must define the eligibility owner exactly once")
    for fragment in _ROOT_CONTEXT_OWNER_FRAGMENTS:
        if fragment not in owner_text:
            defects.append(f"{_ROOT_CONTEXT_OWNER} is missing {fragment!r}")
    for fragment in _ROOT_CONTEXT_CLI_FRAGMENTS:
        if fragment not in cli_text:
            defects.append(f"{_ROOT_CONTEXT_CLI} is missing {fragment!r}")

    findings = list(
        line_pattern_violations(
            provider,
            rule_id=rule_id,
            paths=_python_paths(provider, under=_SRC, exclude=(_ROOT_CONTEXT_OWNER,)),
            pattern=_ROOT_CONTEXT_DUPLICATE_PATTERN,
            message=(
                "hand-authored project root write eligibility belongs in "
                "AgentsCompiler._hand_authored_root_context_blocks_write"
            ),
            exempt_marker=None,
        )
    )
    if defects:
        findings.append(
            violation(
                rule_id,
                _ROOT_CONTEXT_OWNER,
                "project root context write eligibility must route through its canonical owner; "
                + "; ".join(defects),
            )
        )
    return findings


def _check_agents_source_attribution(provider: FactsProvider) -> Iterable[Violation]:
    """AGENTS.md cosmetics must use the canonical ``source_attribution`` boolean.

    Ports ``scripts/check_agents_source_attribution_owner.py``: inside
    ``DistributedAgentsCompiler.compile_distributed`` the manifest boolean must
    be bound from ``config.get("source_attribution")`` and forwarded unchanged
    as ``source_attribution=source_attribution`` to ``_generate_agents_content``
    rather than the placement source map. The structural analysis reads the
    ``(node, parent)`` facts captured by the one shared traversal.
    """
    rule_id = "registry_delegation.agents_source_attribution"
    _facts, failures = checked_facts(
        provider, registry_legacy.DISTRIBUTED_COMPILER_PATH, rule_id, require_python=True
    )
    if failures:
        return failures
    index = provider.tree_index(registry_legacy.DISTRIBUTED_COMPILER_PATH)
    return _as_violations(rule_id, registry_legacy.agents_source_attribution_findings(index))


def _check_lockfile_version_authority(provider: FactsProvider) -> Iterable[Violation]:
    """Lockfile supported-version authority belongs in ``deps/lockfile.py``.

    Ports the AC2 ``check_pattern``: no module under ``src/apm_cli`` other than
    the lockfile owner may name ``SUPPORTED_LOCKFILE_VERSIONS`` or compare a
    ``lockfile_version`` against anything.
    """
    rule_id = "registry_delegation.lockfile_version_authority"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_python_paths(provider, under=_SRC, exclude=(_LOCKFILE_OWNER,)),
        pattern=_LOCKFILE_VERSION_PATTERN,
        message="lockfile supported-version authority belongs in deps/lockfile.py",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_command_machine_output(provider: FactsProvider) -> Iterable[Violation]:
    """Machine-output routing belongs at the root CLI, not in a command.

    Ports the AC5 ``check_pattern`` forbidding ``set_console_stderr`` anywhere
    under ``src/apm_cli/commands``.
    """
    rule_id = "registry_delegation.command_machine_output"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_python_paths(provider, under=_COMMANDS_PREFIX),
        pattern=_CONSOLE_STDERR_PATTERN,
        message="machine-output routing belongs at the root CLI",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_logger_redaction_attachment(provider: FactsProvider) -> Iterable[Violation]:
    """Secret redaction must attach to handlers, not package loggers.

    Ports the AC5 ``check_pattern`` forbidding a filter attached to the
    ``apm_cli`` package logger in the root CLI module.
    """
    rule_id = "registry_delegation.logger_redaction_attachment"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=(_ROOT_CLI,),
        pattern=_PACKAGE_LOGGER_FILTER_PATTERN,
        message="secret redaction must attach to handlers, not package loggers",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_root_cli_output_mode(provider: FactsProvider) -> Iterable[Violation]:
    """Root CLI must establish machine mode and handler-level redaction.

    Ports the AC5 paired ``grep -q`` guard: ``cli.py`` must both detect the
    output mode and attach the redaction filter to a handler. The legacy guard
    emitted one message for either omission, so this rule reports one violation
    naming whichever leg is missing.
    """
    rule_id = "registry_delegation.root_cli_output_mode"
    facts, failures = checked_facts(provider, _ROOT_CLI, rule_id, require_python=True)
    if failures:
        return failures
    missing = tuple(
        pattern for pattern in _ROOT_CLI_REQUIRED_PATTERNS if not _has_regex(facts, pattern)
    )
    if not missing:
        return ()
    joined = ", ".join(repr(pattern) for pattern in missing)
    return (
        violation(
            rule_id,
            _ROOT_CLI,
            f"root CLI must establish machine mode and handler-level redaction; missing: {joined}",
        ),
    )


def _check_policy_ref_redaction(provider: FactsProvider) -> Iterable[Violation]:
    """Policy cache metadata must redact URL credentials at its canonical writer.

    Ports the AC5 paired ``grep -q`` guard over ``policy/discovery.py``: both
    the single ``repo_ref`` and the ``chain_refs`` comprehension must pass
    through ``_redact_policy_ref``.
    """
    rule_id = "registry_delegation.policy_ref_redaction"
    return require_text(
        provider,
        rule_id=rule_id,
        path=_POLICY_DISCOVERY,
        needles=_POLICY_REDACTION_REQUIRED,
        message="policy cache metadata must redact URL credentials at its canonical writer",
    )


def _check_manifest_schema_negotiation(provider: FactsProvider) -> Iterable[Violation]:
    """Manifest schema negotiation belongs in ``models/manifest_contract.py``.

    The legacy shell expression was accidentally over-escaped and could not
    match a real ``.get("$schema")`` call.  The semantic rule applies the
    intended expression while allowing the manifest-contract owner and the two
    Agent Plugin contract readers that own their distinct schema boundary.
    """
    rule_id = "registry_delegation.manifest_schema_negotiation"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_python_paths(provider, under=_SRC, exclude=_MANIFEST_SCHEMA_OWNERS),
        pattern=_MANIFEST_SCHEMA_PATTERN,
        message="manifest schema negotiation belongs in models/manifest_contract.py",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_lifecycle_docs_aggregate(provider: FactsProvider) -> Iterable[Violation]:
    """Lifecycle docs must keep aggregate compilation explicit.

    Ports the AC6 ``grep -q`` over the published lifecycle concept page, the
    one legacy guard that reads a documentation file rather than source.
    """
    rule_id = "registry_delegation.lifecycle_docs_aggregate"
    facts, failures = checked_facts(provider, _LIFECYCLE_DOC, rule_id)
    if failures:
        return failures
    if _LIFECYCLE_DOC_REQUIRED in source_text(facts):
        return ()
    return (
        violation(
            rule_id,
            _LIFECYCLE_DOC,
            "lifecycle docs must keep aggregate compilation explicit; missing: "
            f"{_LIFECYCLE_DOC_REQUIRED!r}",
        ),
    )


def _check_diagnostic_ascii_owner(provider: FactsProvider) -> Iterable[Violation]:
    """Agent diagnostic names must use ``utils/diagnostics.py::printable_ascii_text``.

    Ports ``scripts/check_diagnostic_ascii_owner.py`` in full: the single owner
    definition, the direct import in both consumers, the owned identity flow in
    each required diagnostic function, the ban on rendering or storing raw
    ``source.name`` / ``package_name``, the ban on routing either through a
    local normalization call, and the module-wide ban on redefining, shadowing,
    restoring, or reimplementing the normalizer. All structural analysis reads
    the ``(node, parent)`` facts captured by the one shared traversal.
    """
    rule_id = "registry_delegation.diagnostic_ascii_owner"
    facts_by_path, failures = _read_required(
        provider, rule_id, registry_legacy.DIAGNOSTIC_ASCII_PATHS
    )
    if failures:
        return failures
    indexes: dict[str, TreeIndex | None] = {
        path: provider.tree_index(path) for path in facts_by_path
    }
    return _as_violations(rule_id, registry_legacy.diagnostic_ascii_findings(indexes))


RULES: tuple[Rule, ...] = (
    Rule(
        id="registry_delegation.host_backend_dispatch",
        group=GROUP,
        guard_ids=(),
        description="Host backend dispatch must come from core/host_providers.py.",
        check=_check_host_backend_dispatch,
    ),
    Rule(
        id="registry_delegation.native_locator_target_names",
        group=GROUP,
        guard_ids=(),
        description="Install orchestration must not branch on native locator target names.",
        check=_check_native_locator_target_names,
    ),
    Rule(
        id="registry_delegation.experimental_target_hints",
        group=GROUP,
        guard_ids=(),
        description="Experimental target hints must route through install/target_hints.py.",
        check=_check_experimental_target_hints,
    ),
    Rule(
        id="registry_delegation.compile_inventory_authority",
        group=GROUP,
        guard_ids=(
            "contracts-tooling-compile-inventory",
            "contracts-tooling-distributed-agents-output",
        ),
        description=(
            "Compile traversal must route through compilation/inventory.py, including nested Git "
            "boundaries."
        ),
        check=_check_compile_inventory_authority,
    ),
    Rule(
        id="registry_delegation.agents_source_attribution",
        group=GROUP,
        guard_ids=(),
        description="AGENTS.md cosmetics must use the canonical source_attribution boolean.",
        check=_check_agents_source_attribution,
    ),
    Rule(
        id="contracts-tooling-root-context-write-eligibility",
        group=GROUP,
        guard_ids=("contracts-tooling-root-context-write-eligibility",),
        description=("Project root context overwrite decisions must route through AgentsCompiler."),
        check=_check_root_context_write_eligibility,
    ),
    Rule(
        id="registry_delegation.lockfile_version_authority",
        group=GROUP,
        guard_ids=(),
        description="Lockfile supported-version authority belongs in deps/lockfile.py.",
        check=_check_lockfile_version_authority,
    ),
    Rule(
        id="registry_delegation.command_machine_output",
        group=GROUP,
        guard_ids=(),
        description="Machine-output routing belongs at the root CLI, not in a command.",
        check=_check_command_machine_output,
    ),
    Rule(
        id="registry_delegation.logger_redaction_attachment",
        group=GROUP,
        guard_ids=(),
        description="Secret redaction must attach to handlers, not package loggers.",
        check=_check_logger_redaction_attachment,
    ),
    Rule(
        id="registry_delegation.root_cli_output_mode",
        group=GROUP,
        guard_ids=(),
        description="Root CLI must establish machine mode and handler-level redaction.",
        check=_check_root_cli_output_mode,
    ),
    Rule(
        id="registry_delegation.policy_ref_redaction",
        group=GROUP,
        guard_ids=(),
        description="Policy cache metadata must redact URL credentials at its writer.",
        check=_check_policy_ref_redaction,
    ),
    Rule(
        id="registry_delegation.manifest_schema_negotiation",
        group=GROUP,
        guard_ids=(),
        description="Manifest schema negotiation belongs in models/manifest_contract.py.",
        check=_check_manifest_schema_negotiation,
    ),
    Rule(
        id="registry_delegation.lifecycle_docs_aggregate",
        group=GROUP,
        guard_ids=(),
        description="Lifecycle docs must keep aggregate compilation explicit.",
        check=_check_lifecycle_docs_aggregate,
    ),
    Rule(
        id="registry_delegation.diagnostic_ascii_owner",
        group=GROUP,
        guard_ids=(),
        description="Agent diagnostic names must use utils/diagnostics printable_ascii_text.",
        check=_check_diagnostic_ascii_owner,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
