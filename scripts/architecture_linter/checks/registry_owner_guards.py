"""Owner-registry-guarded registry/delegation analyzers.

This module ports the ``lint-architecture-boundaries.sh`` guards whose
canonical owners live in ``.apm/architecture/owners/core-runtime.json`` --
the single-owner delegation, output boundary, and validate-before-mutate
authorities for the runtime core:

======================================  ==================================
Owner guard (executed exactly once)     Legacy shell provenance
======================================  ==================================
registry-delegation-runtime-descriptors AC1 "Runtime names must come from
                                        runtime/registry.py"
registry-delegation-target-vocabulary   AC1 "Manifest target consumers must
                                        use canonical_targets"
registry-delegation-install-target-     AC1 "Package, MCP, and LSP phases
selection                               must share EffectiveTargetDecision"
registry-delegation-output-diagnostics  AC12 doctor-status STATUS_SYMBOLS
                                        owner check
registry-delegation-compiled-output-    AC2 "Compiled output writes must use
writes                                  CompiledOutputWriter"
registry-delegation-bootstrap-project-  AC18 ``lint-bootstrap-project-name``
name                                    (ported semantically)
======================================  ==================================

Every rule check consumes only :class:`FactsProvider` inventory and cached
per-file facts: no rule reads a file, parses source, walks an AST, shells
out, or invokes a helper CLI. Fail-closed read/parse behavior is inherited
from :func:`scripts.architecture_linter.groups.common.checked_facts`.

Sibling module :mod:`registry_semantic_rules` carries the legacy
core-runtime units that never had an owner-registry guard allocated to
them; :mod:`registry_shared` carries the helpers both modules call.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

from scripts.architecture_linter.checks.python_semantics import (
    binding_nodes,
    direct_definitions,
    import_bound_name,
)
from scripts.architecture_linter.checks.registry_shared import (
    _SRC,
    GROUP,
    _count_regex_lines,
    _has_regex,
    _python_paths,
    _read_required,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    line_pattern_violations,
    source_text,
    violation,
)
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_RUNTIME_NAME_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/commands/runtime.py",
    "src/apm_cli/core/script_runner.py",
    "src/apm_cli/runtime/manager.py",
    "src/apm_cli/workflow/runner.py",
)


_RUNTIME_NAME_PATTERN = (
    r"click\.Choice\(\[.*(copilot|codex|gemini|llm)|runtime_commands = \[|"
    r'return \["copilot", "codex"'
)


_TARGET_VOCAB_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/bundle/packer.py",
    "src/apm_cli/install/mcp/integration.py",
    "src/apm_cli/commands/uninstall/engine.py",
)


_TARGET_VOCAB_PATTERN = r"(package|apm_package)\.(target|targets)\b"


_EFFECTIVE_TARGET_OWNER = "src/apm_cli/core/target_detection.py"


_INSTALL_CMD = "src/apm_cli/commands/install.py"


_INSTALL_PIPELINE = "src/apm_cli/install/pipeline.py"


_INSTALL_SERVICE = "src/apm_cli/install/service_integration.py"


_UPDATE_CMD = "src/apm_cli/commands/update.py"


_DOCTOR_FILE = "src/apm_cli/commands/marketplace/__init__.py"


_DOCTOR_FUNCTION = "_doctor_status_icon"


_DOCTOR_RAW_SYMBOLS: frozenset[str] = frozenset({"[!]", "[x]", "[i]", "[+]"})


_DOCTOR_OWNER_NAME = "STATUS_SYMBOLS"


_COMPILATION_PREFIX = "src/apm_cli/compilation/"


_COMPILED_OUTPUT_OWNER = "src/apm_cli/compilation/output_writer.py"


_COMPILED_WRITE_PATTERN = r'write_text_lf|atomic_write_text|\.write_text\(|open\([^)]*["\']w'


_PROJECT_NAME_OWNER = "src/apm_cli/core/project_name.py"


_BOOTSTRAP_INIT = "src/apm_cli/commands/init.py"


_BOOTSTRAP_INSTALL = "src/apm_cli/commands/install.py"


_BOOTSTRAP_RUNNER = "src/apm_cli/core/script_runner.py"


_BOOTSTRAP_DEPS_CLI = "src/apm_cli/commands/deps/cli.py"


_BOOTSTRAP_CONSTANT = "DEFAULT_BOOTSTRAP_PROJECT_NAME"


_BOOTSTRAP_CONSTANT_VALUE = "my-project"


_BOOTSTRAP_OWNED_DEFS: tuple[str, ...] = (
    "validate_project_name",
    "resolve_bootstrap_project_name",
)


_RESOLVER_ALT = r"_?resolve_bootstrap_project_name"


def _count_fixed_lines(facts: FileFacts, needle: str) -> int:
    """Count lexical lines containing `needle` (mirrors ``grep -Fc``)."""
    return sum(1 for line in facts.lines if needle in line)


def _has_fixed(facts: FileFacts, needle: str) -> bool:
    """Return whether any lexical line contains `needle` (``grep -q``)."""
    return needle in source_text(facts)


def _defining_files(
    provider: FactsProvider,
    rule_id: str,
    name: str,
    *,
    kinds: tuple[str, ...],
) -> tuple[frozenset[str], tuple[Violation, ...]]:
    """Return the set of source files defining `name`, failing closed on parse.

    Scans every ``src/apm_cli`` Python module in the deterministic inventory
    and reports which ones declare a top-level-or-nested definition named
    `name` of one of the accepted `kinds`. A read/parse failure on any file
    aborts the scan with a fail-closed violation, mirroring the original
    bootstrap linter, which parsed every module and crashed on a bad tree.
    """
    definers: set[str] = set()
    failures: list[Violation] = []
    for path in _python_paths(provider, under=_SRC):
        facts, read_failures = checked_facts(provider, path, rule_id, require_python=True)
        if read_failures:
            failures.extend(read_failures)
            continue
        if any(
            definition.name == name and definition.kind in kinds for definition in facts.definitions
        ):
            definers.add(path)
    return frozenset(definers), tuple(failures)


def _check_runtime_descriptors(provider: FactsProvider) -> Iterable[Violation]:
    """Runtime names must be sourced from ``runtime/registry.py`` only.

    Ports the AC1 ``check_pattern`` forbidding hard-coded runtime-name
    vocabularies (Click choices, literal command lists) in the four runtime
    consumer modules.
    """
    rule_id = "registry_delegation.runtime_descriptors"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_RUNTIME_NAME_CONSUMERS,
        pattern=_RUNTIME_NAME_PATTERN,
        message="runtime names must come from runtime/registry.py",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_target_vocabulary(provider: FactsProvider) -> Iterable[Violation]:
    """Manifest target consumers must route through ``canonical_targets``.

    Ports the AC1 ``check_pattern`` forbidding raw ``package.target`` /
    ``apm_package.targets`` attribute reads in the three manifest-target
    consumer modules.
    """
    rule_id = "registry_delegation.target_vocabulary"
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=_TARGET_VOCAB_CONSUMERS,
        pattern=_TARGET_VOCAB_PATTERN,
        message="manifest target consumers must use canonical_targets",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_install_target_selection(provider: FactsProvider) -> Iterable[Violation]:
    """Package, MCP, and LSP phases must share one ``EffectiveTargetDecision``.

    Ports the AC1 composite guard: the owner defines
    ``resolve_effective_target_decision`` exactly once, the pipeline and both
    install/update consumers thread the resulting ``target_decision`` through
    their canonical fields, the service integrator forwards it at least
    twice, the raw ``explicit_target=ctx.target`` seam appears exactly once,
    and no consumer smuggles a parallel ``target_context=(... ctx.target``
    tuple.
    """
    rule_id = "registry_delegation.install_target_selection"
    paths = (
        _EFFECTIVE_TARGET_OWNER,
        _INSTALL_CMD,
        _INSTALL_PIPELINE,
        _INSTALL_SERVICE,
        _UPDATE_CMD,
    )
    facts_by_path, failures = _read_required(provider, rule_id, paths)
    if failures:
        return failures

    findings: list[Violation] = []
    owner = facts_by_path[_EFFECTIVE_TARGET_OWNER]
    install = facts_by_path[_INSTALL_CMD]
    pipeline = facts_by_path[_INSTALL_PIPELINE]
    service = facts_by_path[_INSTALL_SERVICE]
    update = facts_by_path[_UPDATE_CMD]

    if _count_regex_lines(owner, r"^def resolve_effective_target_decision\(") != 1:
        findings.append(
            violation(
                rule_id,
                _EFFECTIVE_TARGET_OWNER,
                "resolve_effective_target_decision must be defined exactly once by "
                "the effective-target owner",
            )
        )
    if not _has_fixed(pipeline, "target_decision = resolve_effective_target_decision("):
        findings.append(
            violation(
                rule_id,
                _INSTALL_PIPELINE,
                "install pipeline must resolve the shared EffectiveTargetDecision",
            )
        )
    if not _has_fixed(install, "ctx.target_decision = install_result.target_decision"):
        findings.append(
            violation(
                rule_id,
                _INSTALL_CMD,
                "install command must persist the resolved target_decision on ctx",
            )
        )
    if not _has_fixed(install, "target_decision=ctx.target_decision"):
        findings.append(
            violation(
                rule_id,
                _INSTALL_CMD,
                "install command must forward ctx.target_decision to downstream phases",
            )
        )
    if _count_fixed_lines(service, "target_decision=target_decision") < 2:
        findings.append(
            violation(
                rule_id,
                _INSTALL_SERVICE,
                "service integration must forward target_decision to package and MCP phases",
            )
        )
    if not _has_fixed(update, 'target_decision = getattr(result, "target_decision", None)'):
        findings.append(
            violation(
                rule_id,
                _UPDATE_CMD,
                "update command must read the resolved target_decision from the result",
            )
        )
    raw_count = _count_regex_lines(
        install, r"explicit_target=ctx\.target( or ctx\.runtime)?([,)]|$)"
    )
    if raw_count != 1:
        findings.append(
            violation(
                rule_id,
                _INSTALL_CMD,
                "the raw explicit_target=ctx.target seam must appear exactly once",
            )
        )
    for number, text in enumerate(install.lines, start=1):
        match = re.search(r"target_context=\([^)]*ctx\.target", text)
        if match is not None:
            findings.append(
                violation(
                    rule_id,
                    _INSTALL_CMD,
                    "parallel target_context tuple must not re-derive ctx.target",
                    line=number,
                    column=match.start() + 1,
                )
            )
    return findings


def _check_output_diagnostics(provider: FactsProvider) -> Iterable[Violation]:
    """Doctor status symbols must come from ``utils/console.py::STATUS_SYMBOLS``.

    Ports the AC12 doctor-status guard: within ``_doctor_status_icon`` no raw
    status glyph may appear as a string constant, the name must come from the
    console owner, and each actual return expression must consume that owner.
    """
    rule_id = "registry_delegation.output_diagnostics"
    _facts, failures = checked_facts(provider, _DOCTOR_FILE, rule_id, require_python=True)
    if failures:
        return failures

    index = provider.tree_index(_DOCTOR_FILE)
    definitions = (
        () if index is None else direct_definitions(index, _DOCTOR_FUNCTION, kinds=FUNCTION_NODES)
    )
    if not definitions:
        return (
            violation(
                rule_id,
                _DOCTOR_FILE,
                "doctor status owner function _doctor_status_icon is missing",
            ),
        )

    findings: list[Violation] = []
    function = definitions[-1]
    if len(definitions) != 1:
        findings.append(
            violation(
                rule_id,
                _DOCTOR_FILE,
                f"{_DOCTOR_FUNCTION} must be defined exactly once, found {len(definitions)}",
                line=function.lineno,
            )
        )

    canonical_imports = tuple(
        node
        for node in index.nodes
        if isinstance(node, ast.ImportFrom)
        and index.definition_anchor(node) is None
        and (
            (node.level == 3 and node.module == "utils.console")
            or (node.level == 0 and node.module == "apm_cli.utils.console")
        )
        and any(alias.name == _DOCTOR_OWNER_NAME and alias.asname is None for alias in node.names)
    )
    owner_imports = tuple(
        node
        for node in index.nodes
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(import_bound_name(alias) == _DOCTOR_OWNER_NAME for alias in node.names)
    )
    if len(canonical_imports) != 1 or owner_imports != canonical_imports:
        offender = (owner_imports or canonical_imports or (function,))[-1]
        findings.append(
            violation(
                rule_id,
                _DOCTOR_FILE,
                "STATUS_SYMBOLS must be imported directly from apm_cli.utils.console",
                line=max(getattr(offender, "lineno", 1), 1),
            )
        )

    canonical_import_ids = {id(node) for node in canonical_imports}
    rebindings = tuple(
        node
        for node in binding_nodes(index, _DOCTOR_OWNER_NAME)
        if id(node) not in canonical_import_ids
    )
    if rebindings:
        findings.append(
            violation(
                rule_id,
                _DOCTOR_FILE,
                "STATUS_SYMBOLS must not be rebound or shadowed",
                line=max(getattr(rebindings[0], "lineno", 1), 1),
            )
        )

    function_nodes = index.own_scope(function)
    for literal in function_nodes:
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            continue
        if literal.value in _DOCTOR_RAW_SYMBOLS:
            findings.append(
                violation(
                    rule_id,
                    _DOCTOR_FILE,
                    "doctor status symbols must use utils/console.py::STATUS_SYMBOLS",
                    line=literal.lineno,
                    column=literal.col_offset + 1,
                )
            )

    returns = tuple(node for node in function_nodes if isinstance(node, ast.Return))
    if not returns or any(not _status_symbol_return(node.value) for node in returns):
        findings.append(
            violation(
                rule_id,
                _DOCTOR_FILE,
                "every doctor status return must consume utils/console.py::STATUS_SYMBOLS",
                line=function.lineno,
            )
        )
    return findings


def _status_symbol_return(node: ast.AST | None) -> bool:
    """Return whether every branch leaf is a canonical STATUS_SYMBOLS lookup."""
    if isinstance(node, ast.IfExp):
        return _status_symbol_return(node.body) and _status_symbol_return(node.orelse)
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == _DOCTOR_OWNER_NAME
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in {"warning", "error", "info", "check"}
    )


def _check_compiled_output_writes(provider: FactsProvider) -> Iterable[Violation]:
    """Compiled-output writes must route through ``CompiledOutputWriter``.

    Ports the AC2 duplicate scan: every ``*.py`` under ``compilation/``
    except the canonical ``output_writer.py`` must be free of direct
    text-write call shapes (``write_text_lf``, ``atomic_write_text``,
    ``.write_text(``, or an ``open(..., "w"...)``).
    """
    rule_id = "registry_delegation.compiled_output_writes"
    paths = _python_paths(
        provider,
        under=_COMPILATION_PREFIX,
        exclude=(_COMPILED_OUTPUT_OWNER,),
    )
    return line_pattern_violations(
        provider,
        rule_id=rule_id,
        paths=paths,
        pattern=_COMPILED_WRITE_PATTERN,
        message="compiled output writes must use CompiledOutputWriter",
        exempt_marker=EXEMPT_MARKER,
    )


def _check_bootstrap_project_name(provider: FactsProvider) -> Iterable[Violation]:
    """Manifest bootstrap names must route through ``core/project_name.py``.

    Semantically ports ``lint-bootstrap-project-name.py``: the canonical
    fallback constant is pinned, both owned functions are defined only by the
    owner (definition-uniqueness across the whole ``src/apm_cli`` tree), and
    each bootstrap consumer assigns the resolver result (or the canonical
    constant) rather than re-deriving a name.
    """
    rule_id = "registry_delegation.bootstrap_project_name"
    consumers = (
        _PROJECT_NAME_OWNER,
        _BOOTSTRAP_INIT,
        _BOOTSTRAP_INSTALL,
        _BOOTSTRAP_RUNNER,
        _BOOTSTRAP_DEPS_CLI,
    )
    facts_by_path, failures = _read_required(provider, rule_id, consumers)
    if failures:
        return failures

    findings: list[Violation] = []
    owner = facts_by_path[_PROJECT_NAME_OWNER]

    constant_pattern = re.compile(
        rf"^{re.escape(_BOOTSTRAP_CONSTANT)}\s*=\s*"
        rf'(?:"{re.escape(_BOOTSTRAP_CONSTANT_VALUE)}"|\'{re.escape(_BOOTSTRAP_CONSTANT_VALUE)}\')'
        r"\s*$"
    )
    if not _has_regex(owner, constant_pattern):
        findings.append(
            violation(
                rule_id,
                _PROJECT_NAME_OWNER,
                "canonical fallback constant DEFAULT_BOOTSTRAP_PROJECT_NAME must equal 'my-project'",
            )
        )

    for name in _BOOTSTRAP_OWNED_DEFS:
        definers, def_failures = _defining_files(provider, rule_id, name, kinds=("function",))
        if def_failures:
            findings.extend(def_failures)
            continue
        if definers != frozenset({_PROJECT_NAME_OWNER}):
            findings.append(
                violation(
                    rule_id,
                    _PROJECT_NAME_OWNER,
                    f"{name} must be defined only by core/project_name.py",
                )
            )

    init_index = provider.tree_index(_BOOTSTRAP_INIT)
    if not _has_resolver_assignment(
        init_index,
        target_name="final_project_name",
        argument_name="derived_project_name",
    ):
        findings.append(
            violation(
                rule_id,
                _BOOTSTRAP_INIT,
                "init bootstrap must assign the resolver result to final_project_name",
            )
        )
    install_index = provider.tree_index(_BOOTSTRAP_INSTALL)
    if not _has_resolver_assignment(
        install_index,
        target_name="project_name",
        argument_name="derived_project_name",
    ):
        findings.append(
            violation(
                rule_id,
                _BOOTSTRAP_INSTALL,
                "install bootstrap must assign the resolver result to project_name",
            )
        )
    runner_index = provider.tree_index(_BOOTSTRAP_RUNNER)
    if not _is_resolver_call(_minimal_config_name(runner_index)):
        findings.append(
            violation(
                rule_id,
                _BOOTSTRAP_RUNNER,
                "ScriptRunner bootstrap name must be the resolver result",
            )
        )
    deps_index = provider.tree_index(_BOOTSTRAP_DEPS_CLI)
    if not _has_named_assignment(
        deps_index,
        target_name="project_name",
        value_name=_BOOTSTRAP_CONSTANT,
    ):
        findings.append(
            violation(
                rule_id,
                _BOOTSTRAP_DEPS_CLI,
                "dependency tree fallback must use the canonical constant",
            )
        )
    return findings


def _is_name(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_resolver_call(node: ast.AST | None, argument_name: str | None = None) -> bool:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or re.fullmatch(_RESOLVER_ALT, node.func.id) is None
    ):
        return False
    return argument_name is None or (len(node.args) == 1 and _is_name(node.args[0], argument_name))


def _has_resolver_assignment(
    index: TreeIndex | None,
    *,
    target_name: str,
    argument_name: str,
) -> bool:
    if index is None:
        return False
    return any(
        isinstance(node, ast.Assign)
        and any(_is_name(target, target_name) for target in node.targets)
        and _is_resolver_call(node.value, argument_name)
        for node in index.nodes
    )


def _minimal_config_name(index: TreeIndex | None) -> ast.AST | None:
    if index is None:
        return None
    functions = tuple(
        function
        for function in index.functions()
        if getattr(function, "name", None) == "_create_minimal_config"
    )
    if len(functions) != 1:
        return None
    function = functions[0]
    for node in index.walk(function):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "name":
                return value
    return None


def _has_named_assignment(
    index: TreeIndex | None,
    *,
    target_name: str,
    value_name: str,
) -> bool:
    if index is None:
        return False
    return any(
        isinstance(node, ast.Assign)
        and any(_is_name(target, target_name) for target in node.targets)
        and _is_name(node.value, value_name)
        for node in index.nodes
    )


RULES: tuple[Rule, ...] = (
    Rule(
        id="registry_delegation.runtime_descriptors",
        group=GROUP,
        guard_ids=("registry-delegation-runtime-descriptors",),
        description="Runtime-name vocabularies must come from runtime/registry.py.",
        check=_check_runtime_descriptors,
    ),
    Rule(
        id="registry_delegation.target_vocabulary",
        group=GROUP,
        guard_ids=("registry-delegation-target-vocabulary",),
        description="Manifest target consumers must use core/target_catalog canonical targets.",
        check=_check_target_vocabulary,
    ),
    Rule(
        id="registry_delegation.install_target_selection",
        group=GROUP,
        guard_ids=("registry-delegation-install-target-selection",),
        description="Package, MCP, and LSP phases must share one EffectiveTargetDecision.",
        check=_check_install_target_selection,
    ),
    Rule(
        id="registry_delegation.output_diagnostics",
        group=GROUP,
        guard_ids=("registry-delegation-output-diagnostics",),
        description="User-facing status symbols must come from utils/console.py STATUS_SYMBOLS.",
        check=_check_output_diagnostics,
    ),
    Rule(
        id="registry_delegation.compiled_output_writes",
        group=GROUP,
        guard_ids=("registry-delegation-compiled-output-writes",),
        description="Compiled-output writes must route through CompiledOutputWriter.",
        check=_check_compiled_output_writes,
    ),
    Rule(
        id="registry_delegation.bootstrap_project_name",
        group=GROUP,
        guard_ids=("registry-delegation-bootstrap-project-name",),
        description="Bootstrap project names must route through core/project_name.py.",
        check=_check_bootstrap_project_name,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
