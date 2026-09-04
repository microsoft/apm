"""Contracts for the single-pass fact cache and the rule-orchestration engine.

Covers, in order:

* :class:`SourceCache` / :class:`ParseCache` -- read-once / parse-once
  memoization, including error caching.
* :class:`FactsProvider` / the shared composite AST traversal -- definitions,
  imports, calls, assignments, literals, scope, parent tracking, registered
  collectors, and the AST-visit counter, all from one walk.
* :data:`runner.GROUP_MODULE_NAMES` and :func:`runner.registered_rules` --
  the explicit cohesive-group catalog and its bidirectional guard equivalence
  with the owner registry.
* :func:`runner.run` aggregation and fail-closed mechanics -- missing
  groups, duplicate rule/guard IDs, unknown rule selection, and every
  malformed-result category -- exercised through fully synthetic,
  monkeypatched rule groups so these engine-mechanics tests stay fast and
  independent of the real 103-rule catalog.
* :mod:`scripts.architecture_linter.diagnostics` -- deterministic ordering
  and strict ASCII ``path:line:column: rule-id message`` rendering.
* The Python-only ``selected_rule_ids`` test API, exercised against the
  real registered rules.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import json
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from scripts.architecture_linter import runner
from scripts.architecture_linter.diagnostics import (
    LEGACY_AC_ALIASES,
    DiagnosticCollector,
    format_failure,
    format_violation,
    render_violations_and_failures,
)
from scripts.architecture_linter.facts import FactsProvider, ParseCache, SourceCache, VisitContext
from scripts.architecture_linter.inventory import build_inventory
from scripts.architecture_linter.models import Failure, FileFacts, Rule, RunReport, Violation
from scripts.architecture_linter.registry import load_registry

REAL_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Section A: SourceCache / ParseCache -- read once, parse once, cache errors.
# ---------------------------------------------------------------------------


@pytest.mark.windows_compat
def test_source_cache_reads_each_path_once_including_errors(tmp_path: Path) -> None:
    """A valid file, a missing file, and an undecodable file each read once."""
    (tmp_path / "ok.py").write_bytes(b"x = 1\n")
    (tmp_path / "bad_utf8.py").write_bytes(b"x = 1\n\xff\xfe")
    cache = SourceCache(tmp_path, ("ok.py", "bad_utf8.py", "missing.py"))

    first = [cache.read("ok.py") for _ in range(3)]
    missing = [cache.read("missing.py") for _ in range(3)]
    undecodable = [cache.read("bad_utf8.py") for _ in range(2)]

    assert all(result == ("x = 1\n", None) for result in first)
    assert all(result[0] is None and "cannot read" in result[1] for result in missing)
    assert all(
        result[0] is None and "cannot decode as utf-8" in result[1] for result in undecodable
    )
    assert cache.read_attempts == 3
    assert cache.read_successes == 1
    assert cache.read_errors == 2
    assert cache.max_reads_per_file == 1
    assert cache.errors == (
        ("bad_utf8.py", undecodable[0][1]),
        ("missing.py", missing[0][1]),
    )


def test_source_cache_rejects_paths_outside_inventory_and_repository(tmp_path: Path) -> None:
    """Inventory membership and resolved containment are required before I/O."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("never expose this", encoding="utf-8")
    cache = SourceCache(
        tmp_path,
        ("not-listed.py", f"../{outside.name}"),
    )

    not_listed = cache.read("absent-from-inventory.py")
    traversal = cache.read(f"../{outside.name}")

    assert not_listed == (None, "path is outside repository inventory")
    assert traversal[0] is None
    assert "unsafe repository-relative path" in (traversal[1] or "")
    assert "never expose this" not in repr(cache.errors)


def test_source_cache_allows_only_safe_test_scoped_overrides(tmp_path: Path) -> None:
    """A safe in-memory override may be outside inventory; traversal may not."""
    cache = SourceCache(
        tmp_path,
        (),
        {
            "fixture/new.py": "x = 1\n",
            "../outside.py": "secret = True\n",
        },
    )

    assert cache.read("fixture/new.py") == ("x = 1\n", None)
    text, error = cache.read("../outside.py")
    assert text is None
    assert "unsafe repository-relative path" in (error or "")


def test_parse_cache_parses_each_source_once_including_syntax_errors() -> None:
    """A valid source and a syntactically invalid source each parse once."""
    cache = ParseCache()

    valid_first = cache.parse("ok.py", "x = 1\n")
    valid_second = cache.parse("ok.py", "x = 1\n")
    # A later call with *different* text for the same path must still be
    # served from the cache -- parsing is memoized by path, not by content.
    valid_third = cache.parse("ok.py", "this is not the same source at all")
    invalid_first = cache.parse("broken.py", "def f(:\n    pass\n")
    invalid_second = cache.parse("broken.py", "def f(:\n    pass\n")

    assert valid_first[1] is None
    assert isinstance(valid_first[0], ast.Module)
    assert valid_second is valid_first
    assert valid_third is valid_first
    assert invalid_first[0] is None
    assert isinstance(invalid_first[1], str)
    assert invalid_second is invalid_first
    assert cache.parse_attempts == 2
    assert cache.parse_successes == 1
    assert cache.parse_errors == 1
    assert cache.max_parses_per_file == 1
    assert cache.errors == (("broken.py", invalid_first[1]),)


# ---------------------------------------------------------------------------
# Section B: FactsProvider / the shared composite AST traversal.
# ---------------------------------------------------------------------------

_SAMPLE_SOURCE = '''"""Module doc."""
import os
from collections import OrderedDict as OD

CONST = "hello"


class Widget:
    count = 0

    def render(self, items):
        items.append(1)
        self.count += 1
        return len(items)


def helper(x):
    y: int = x
    y += 1
    return y
'''


class _RecordingCollector:
    """A minimal :class:`~scripts.architecture_linter.facts.Collector`."""

    name = "recording"

    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[VisitContext] = []

    def on_node(self, context: VisitContext) -> list[str]:
        self.calls += 1
        self.contexts.append(context)
        return [f"{type(context.node).__name__}@{context.scope}"]


def _build_sample_provider(tmp_path: Path) -> tuple[FactsProvider, _RecordingCollector, str]:
    (tmp_path / "sample.py").write_text(_SAMPLE_SOURCE, encoding="utf-8")
    collector = _RecordingCollector()
    provider = FactsProvider(tmp_path, ("sample.py",), registry=None, collectors=(collector,))
    return provider, collector, "sample.py"


def test_composite_facts_capture_definitions_imports_calls_assignments_and_literals(
    tmp_path: Path,
) -> None:
    """One shared walk yields every fact kind the engine promises rules."""
    provider, _collector, path = _build_sample_provider(tmp_path)

    facts = provider.file_facts(path)

    assert [(d.name, d.kind, d.scope) for d in facts.definitions] == [
        ("Widget", "class", "<module>"),
        ("render", "function", "Widget"),
        ("helper", "function", "<module>"),
    ]
    assert [(i.module, i.names, i.level, i.scope) for i in facts.imports] == [
        (None, ("os",), 0, "<module>"),
        ("collections", ("OrderedDict",), 0, "<module>"),
    ]
    assert [(c.qualname, c.scope) for c in facts.calls] == [
        ("items.append", "render"),
        ("len", "render"),
    ]
    assert [(a.target, a.kind, a.scope) for a in facts.assignments] == [
        ("CONST", "assign", "<module>"),
        ("count", "assign", "Widget"),
        ("items", "call_mutation", "render"),
        ("self.count", "attribute_aug_assign", "render"),
        ("y", "ann_assign", "helper"),
        ("y", "aug_assign", "helper"),
    ]
    assert [(litfact.value_repr, litfact.kind, litfact.scope) for litfact in facts.literals] == [
        ("'Module doc.'", "str", "<module>"),
        ("'hello'", "str", "<module>"),
    ]


def test_composite_facts_track_scope_and_immediate_ast_parents_via_collectors(
    tmp_path: Path,
) -> None:
    """Registered collectors see the real scope stack and parent chain."""
    provider, collector, path = _build_sample_provider(tmp_path)

    provider.file_facts(path)

    def parent_kind(node_kind: str, **attrs: object) -> str | None:
        """Look up the one recorded node of `node_kind` matching `attrs`.

        Matches by attribute predicate (e.g. `lineno=`, `id=`) instead of a
        positional index: the shared traversal visits nodes in depth-first
        pre-order, which is *not* the order `ast.walk` (breadth-first) would
        suggest, so only a predicate on the node itself is unambiguous.
        """
        for ctx in collector.contexts:
            if type(ctx.node).__name__ != node_kind:
                continue
            if all(getattr(ctx.node, key, None) == value for key, value in attrs.items()):
                return type(ctx.parent).__name__ if ctx.parent is not None else None
        raise AssertionError(f"no {node_kind} node found matching {attrs}")

    module_contexts = [ctx for ctx in collector.contexts if type(ctx.node).__name__ == "Module"]
    assert len(module_contexts) == 1
    assert module_contexts[0].parent is None

    # `items.append(1)` at line 12: Call -> Attribute -> Name, one parent each.
    assert parent_kind("Call", lineno=12) == "Expr"
    assert parent_kind("Attribute", lineno=12) == "Call"
    assert parent_kind("Name", lineno=12, id="items") == "Attribute"

    # `self.count += 1` at line 13: the attribute target is parented to AugAssign.
    assert parent_kind("Attribute", lineno=13) == "AugAssign"

    # `return len(items)` at line 14: the outer Call is parented to Return.
    assert parent_kind("Call", lineno=14) == "Return"


def test_facts_provider_builds_one_compact_tree_index_per_file(tmp_path: Path) -> None:
    """Repeated analyzer queries reuse one index and never copy an index per rule."""
    (tmp_path / "sample.py").write_text(_SAMPLE_SOURCE, encoding="utf-8")
    provider = FactsProvider(tmp_path, ("sample.py",), registry=None)

    first = provider.tree_index("sample.py")
    second = provider.tree_index("sample.py")

    assert first is second
    assert first is not None
    assert provider.tree_index_builds == 1
    assert provider.tree_index_cache_hits == 1
    assert provider.max_tree_index_builds_per_file == 1
    assert provider.peak_tree_index_nodes == len(first.nodes)
    assert not hasattr(first, "_descendants")


def test_spill_write_failure_keeps_facts_without_rewalking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cold-fact spill fails closed but leaves one reusable build."""
    path = "tests/test_sample.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(_SAMPLE_SOURCE, encoding="utf-8")
    provider = FactsProvider(tmp_path, (path,), registry=None)

    def fail_spill(_relative_path: str, _facts: FileFacts) -> None:
        raise OSError("injected spill failure")

    monkeypatch.setattr(provider, "_spill_facts", fail_spill)

    with pytest.raises(OSError, match="injected spill failure"):
        provider.file_facts(path)
    facts = provider.file_facts(path)

    assert facts.tree_index is not None
    assert provider.source_cache.read_attempts == 1
    assert provider.parse_cache.parse_attempts == 1
    assert provider.ast_visits == facts.visits
    assert provider.tree_index_builds == 1
    assert provider.max_tree_index_builds_per_file == 1


def test_spill_load_failure_never_rebuilds_or_hides_the_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed or unreadable spill state remains a fail-closed cache state."""
    path = "tests/test_sample.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(_SAMPLE_SOURCE, encoding="utf-8")
    provider = FactsProvider(tmp_path, (path,), registry=None)
    facts = provider.file_facts(path)
    provider._transient_facts.clear()

    def fail_load(_spill_path: Path) -> FileFacts:
        raise OSError("injected spill load failure")

    monkeypatch.setattr(provider, "_load_spilled_facts", fail_load)

    for _ in range(2):
        with pytest.raises(OSError, match="injected spill load failure"):
            provider.file_facts(path)

    assert provider.source_cache.read_attempts == 1
    assert provider.parse_cache.parse_attempts == 1
    assert provider.ast_visits == facts.visits
    assert provider.tree_index_builds == 1
    assert provider.max_tree_index_builds_per_file == 1


def test_spill_reload_preserves_nested_function_qualnames(tmp_path: Path) -> None:
    """Stable pre-order keys survive pickle replacing every AST node identity."""
    path = "tests/test_nested.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(
        """
class Container:
    def method(self):
        def inner():
            return 1
        return inner()
""".lstrip(),
        encoding="utf-8",
    )
    provider = FactsProvider(tmp_path, (path,), registry=None)

    original = provider.file_facts(path).tree_index
    assert original is not None
    original_inner = original.function("Container.method.inner")
    assert original_inner is not None
    provider._transient_facts.clear()

    reloaded = provider.file_facts(path).tree_index
    assert reloaded is not None
    reloaded_inner = reloaded.function("Container.method.inner")
    assert reloaded_inner is not None
    assert reloaded_inner is not original_inner
    assert reloaded.function_qualname(reloaded_inner) == "Container.method.inner"
    assert provider.source_cache.read_attempts == 1
    assert provider.parse_cache.parse_attempts == 1
    assert provider.tree_index_builds == 1


@pytest.mark.windows_compat
def test_tree_index_build_has_one_production_owner() -> None:
    """Only FactsProvider may fold FileFacts into a compact TreeIndex."""
    linter_root = REAL_ROOT / "scripts/architecture_linter"
    callers: list[str] = []
    for path in sorted(linter_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "build_tree_index":
                callers.append(path.relative_to(REAL_ROOT).as_posix())

    assert callers == [
        "scripts/architecture_linter/facts.py",
    ]


def test_retired_architecture_checker_entrypoints_cannot_return() -> None:
    """The registered catalog is the only executable architecture authority."""
    retired = {
        "check_agent_plugin_component_ir.py",
        "check_agent_plugin_projection_boundary.py",
        "check_agents_source_attribution_owner.py",
        "check_bundle_format_authority.sh",
        "check_cleanup_claim_owner.py",
        "check_compile_inventory_authority.py",
        "check_deployment_owner_boundaries.py",
        "check_deployment_state_mutations.py",
        "check_diagnostic_ascii_owner.py",
        "check_generated_bundle_text_writers.py",
        "check_hash_visible_lf_writes.py",
        "check_hook_config_write_owner.py",
        "check_hook_file_routing_owner.py",
        "check_package_target_authority.py",
        "check_plugin_skill_declaration_authority.py",
        "check_removed_agent_plugin_lifecycle.py",
        "check_repository_cache_identity_owner.py",
        "check_shared_target_contraction_owner.py",
        "check_skill_subset_owner.py",
        "check_target_instruction_contraction_owner.py",
        "check_test_contract_authorities.py",
        "check_uninstall_selection_owner.py",
        "check_windows_stable_path_owner.py",
        "lint-bootstrap-project-name.py",
        "lint-resolution-replacement-boundary.py",
    }
    scripts_root = REAL_ROOT / "scripts"
    assert not {path.name for path in scripts_root.iterdir()} & retired

    retired_modules = {name.removesuffix(".py").replace("-", "_") for name in retired}
    imported: set[str] = set()
    for path in sorted((scripts_root / "architecture_linter").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.rsplit(".", 1)[-1])
    assert not imported & retired_modules


def test_ast_visit_counter_matches_an_independent_ast_walk_with_no_extra_traversal(
    tmp_path: Path,
) -> None:
    """The visit counter is exact and collectors ride the one walk, not a second."""
    provider, collector, path = _build_sample_provider(tmp_path)

    facts = provider.file_facts(path)

    independent_count = sum(1 for _ in ast.walk(ast.parse(_SAMPLE_SOURCE)))
    assert facts.visits == independent_count
    assert provider.ast_visits == independent_count
    assert collector.calls == independent_count
    assert len(facts.extra["recording"]) == independent_count

    # Requesting the same file again must not re-walk, re-read, or re-parse.
    facts_again = provider.file_facts(path)
    assert facts_again is facts
    assert collector.calls == independent_count
    assert provider.source_cache.read_attempts == 1
    assert provider.parse_cache.parse_attempts == 1


def test_facts_provider_read_error_short_circuits_ast_facts(tmp_path: Path) -> None:
    """An undecodable Python file yields no facts and zero AST visits."""
    (tmp_path / "bad.py").write_bytes(b"x = 1\n\xff\xfe")
    provider = FactsProvider(tmp_path, ("bad.py",), registry=None)

    facts = provider.file_facts("bad.py")

    assert facts.exists is False
    assert facts.is_python is True
    assert facts.read_error is not None
    assert facts.parse_error is None
    assert facts.lines == ()
    assert (
        facts.definitions,
        facts.imports,
        facts.calls,
        facts.assignments,
        facts.literals,
    ) == ((), (), (), (), ())
    assert facts.visits == 0
    assert provider.parse_cache.parse_attempts == 0


def test_facts_provider_parse_error_short_circuits_ast_facts_but_keeps_lines(
    tmp_path: Path,
) -> None:
    """A readable-but-unparsable Python file keeps lexical lines, not AST facts."""
    (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    provider = FactsProvider(tmp_path, ("broken.py",), registry=None)

    facts = provider.file_facts("broken.py")

    assert facts.exists is True
    assert facts.parse_error is not None
    assert facts.lines == ("def f(:", "    pass")
    assert facts.definitions == ()
    assert facts.visits == 0


def test_facts_provider_skips_ast_parsing_for_non_python_files(tmp_path: Path) -> None:
    """A non-``.py`` file never reaches the parser, even if it would parse fine."""
    (tmp_path / "notes.txt").write_text("x = 1\n", encoding="utf-8")
    provider = FactsProvider(tmp_path, ("notes.txt",), registry=None)

    facts = provider.file_facts("notes.txt")

    assert facts.is_python is False
    assert facts.lines == ("x = 1",)
    assert facts.definitions == ()
    assert facts.visits == 0
    assert provider.parse_cache.parse_attempts == 0


# ---------------------------------------------------------------------------
# Section C: the fixed six-group catalog and its guard equivalence with the
# real 55-guard owner registry.
# ---------------------------------------------------------------------------


def test_explicit_group_catalog_is_cohesive_unique_and_fully_registered() -> None:
    """The explicit 5-7 group catalog loads every registered rule."""
    imports = runner._import_groups()
    names = tuple(name for name, _module, _error in imports)
    rules = runner.registered_rules()

    assert names == runner.GROUP_MODULE_NAMES
    assert 5 <= len(names) <= 7
    assert len(set(names)) == len(names)
    assert all(module is not None and error is None for _name, module, error in imports)
    assert {rule.group for rule in rules} == set(names)


def test_importing_runner_does_not_import_rule_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading the runner is import-safe; groups load only inside a run."""
    real_import = builtins.__import__
    group_imports: list[tuple[str, ...]] = []

    def recording_import(
        name: str,
        globals_: dict[str, object] | None = None,
        locals_: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "scripts.architecture_linter.groups":
            group_imports.append(tuple(fromlist))
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", recording_import)
    importlib.reload(runner)

    assert group_imports == []
    assert runner._GROUP_IMPORTS is None


def test_literal_group_import_converts_system_exit_to_import_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group calling sys.exit(0) cannot terminate or pass the linter."""
    real_import = builtins.__import__

    def exiting_import(
        name: str,
        globals_: dict[str, object] | None = None,
        locals_: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "scripts.architecture_linter.groups" and fromlist == ("registry_delegation",):
            raise SystemExit(0)
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", exiting_import)
    imports = runner._import_groups()

    assert imports[0] == ("registry_delegation", None, "0")
    assert tuple(name for name, _module, _error in imports) == runner.GROUP_MODULE_NAMES

    _write_synthetic_repo(tmp_path, ["keep-guard"])
    keep_rule = Rule(
        "keep-rule",
        "mutation_writes",
        ("keep-guard",),
        "d",
        lambda provider: [],
    )
    runnable = list(_fake_group_imports({"mutation_writes": [keep_rule]}))
    runnable[0] = imports[0]
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", tuple(runnable))

    report = runner.run(tmp_path)

    assert report.exit_code == 1
    assert Failure("import:registry_delegation", "0") in report.failures


_EXPECTED_RULE_ID_TEXT = """
contracts-tests-executable-contract-authorities
contracts-tests-lifecycle-smoke-partition
contracts-tests-taxonomy-classification
contracts-tooling-ado-lock-coordinates
contracts-tooling-apply-to-placement
contracts-tooling-cached-policy-shape
contracts-tooling-dependency-identity
contracts-tooling-frontmatter-yaml
contracts-tooling-generation-footer
contracts-tooling-lockfile-read
contracts-tooling-lockfile-timestamp
contracts-tooling-lockfile-timestamp-constructor
contracts-tooling-lockfile-timestamp-fallback
contracts-tooling-project-yaml-write-delegation
install-deployment-approval-outcome-routing
install-deployment-audit-policy-discovery
install-deployment-audit-replay
install-deployment-bundle-native-layout
install-deployment-base-integrator
install-deployment-cached-claude-skill-metadata
install-deployment-dependency-winner-selection
install-deployment-deployment-frame-projection
install-deployment-executable-trust-context
install-deployment-frozen-mutation-eligibility
install-deployment-git-object-field-authority
install-deployment-gitlab-facade-orchestration
install-deployment-gitlab-policy-adapter
install-deployment-incomplete-chain-routing
install-deployment-install-scope-selection
install-deployment-local-bundle-policy-preflight
install-deployment-local-identity-anchor
install-deployment-locked-skill-subset-reconstruction
install-deployment-lsp-lifecycle
install-deployment-lsp-target-contract
install-deployment-manifest-inheritance-includes
install-deployment-marketplace-mutation-lock
install-deployment-lifecycle-serialization
install-deployment-mcp-ownership-migration
install-deployment-mcp-registry-resolution
install-deployment-outcome
install-deployment-package-target-authorization
install-deployment-plugin-bin-eligibility
install-deployment-primitive-classification
install-deployment-prospective-dry-run-plan
install-deployment-provenance-state
install-deployment-ref-recheck-ownership
install-deployment-registry-dependency-intent
install-deployment-request-defaults
install-deployment-require-hashes-enforcement
install-deployment-resolution-replacement
install-deployment-resolver-queue-dedup
install-deployment-skill-subset-tokens
install-deployment-source-plan
install-deployment-target-file-contraction
install-deployment-uninstall-reachability
install-deployment-uninstall-selection
install-deployment-update-plan-ref-annotation
marketplace-integrations-agent-plugin-contract
marketplace-integrations-bundle-format-authority
marketplace-integrations-catalog-manifest
marketplace-integrations-copilot-ownership
marketplace-integrations-generated-bundle-lf-writers
marketplace-integrations-hash-visible-lf-writers
marketplace-integrations-legacy-skill-membership
marketplace-integrations-local-audit-resolution
marketplace-integrations-metadata-enrichment
marketplace-integrations-native-registration
marketplace-integrations-output-path
marketplace-integrations-package-construction
marketplace-integrations-package-format-precedence
marketplace-integrations-package-projection
marketplace-integrations-producer-admission
marketplace-integrations-projection-boundary
marketplace-integrations-raw-diagnostics
marketplace-integrations-removed-plugin-lifecycle
marketplace-integrations-source-admission
marketplace-integrations-source-parsing
marketplace-integrations-tag-pattern
marketplace-integrations-version-precedence
mutation_writes.copilot_cli_mcp_paths
mutation_writes.drift_hook_membership
mutation_writes.hook_cleanup_scope
mutation_writes.hook_command_vocabulary
mutation_writes.jetbrains_mcp_path
mutation_writes.mcp_declaration_scope
mutation_writes.mcp_package_launcher
mutation_writes.mcp_passthrough_denylist
mutation_writes.mcp_target_selection
mutation_writes.neutral_hook_contract
mutation_writes.user_root_scope
contracts-tooling-root-context-write-eligibility
registry_delegation.agents_source_attribution
registry_delegation.bootstrap_project_name
registry_delegation.command_machine_output
registry_delegation.compile_inventory_authority
registry_delegation.compiled_output_writes
registry_delegation.diagnostic_ascii_owner
registry_delegation.experimental_target_hints
registry_delegation.host_backend_dispatch
registry_delegation.install_target_selection
registry_delegation.lifecycle_docs_aggregate
registry_delegation.lockfile_version_authority
registry_delegation.logger_redaction_attachment
registry_delegation.manifest_schema_negotiation
registry_delegation.native_locator_target_names
registry_delegation.output_diagnostics
registry_delegation.policy_ref_redaction
registry_delegation.root_cli_output_mode
registry_delegation.runtime_descriptors
registry_delegation.target_vocabulary
transport-platform-artifactory-full-commit-sha
transport-platform-artifactory-netrc-isolation
transport-platform-git-cache-identity
transport-platform-git-child-environment
transport-platform-git-semver-preflight
transport-platform-github-throttle
transport-platform-host-credential-resolution
transport-platform-host-reference-coordinates
transport-platform-network-host-parsing
transport-platform-ref-freshness
transport-platform-revision-pin-outcome
transport-platform-runtime-deadline-safety
transport-platform-self-update-resolution
transport-platform-sparse-symlink-validation
transport-platform-tls-trust-injection
transport-platform-url-path-security
transport-platform-windows-stable-path
"""
_EXPECTED_RULE_IDS = frozenset(_EXPECTED_RULE_ID_TEXT.split())


def test_registered_rule_inventory_matches_frozen_semantic_contract() -> None:
    """No owner or guard-less legacy semantic can disappear silently."""
    assert {rule.id for rule in runner.registered_rules()} == _EXPECTED_RULE_IDS


def test_registered_rule_ids_are_unique_and_stable_across_calls() -> None:
    """Every registered rule ID is unique, sorted, and repeatable on re-query."""
    first = runner.registered_rules()
    second = runner.registered_rules()

    ids = [rule.id for rule in first]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert [rule.id for rule in second] == ids
    assert {rule.group for rule in first} == set(runner.GROUP_MODULE_NAMES)


def test_registered_rule_guard_ids_equal_registry_guards() -> None:
    """Rule-declared guards and registry-referenced guards are the same IDs."""
    rules = runner.registered_rules()
    inventory = build_inventory(REAL_ROOT)
    registry = load_registry(REAL_ROOT / ".apm/architecture/owners", inventory.files)

    rule_guard_ids = {guard for rule in rules for guard in rule.guard_ids}
    registry_guard_ids = {guard for owner in registry.owners for guard in owner.guards}

    assert rule_guard_ids == registry_guard_ids
    assert all(len(set(rule.guard_ids)) == len(rule.guard_ids) for rule in rules)


# ---------------------------------------------------------------------------
# Section D: `run()` aggregation and fail-closed mechanics.
#
# These use fully synthetic, monkeypatched rule groups instead of the real
# 103-rule catalog: `runner._GROUP_IMPORTS` is the module-level tuple both
# `run()` and `registered_rules()` read at call time, so replacing it (and
# restoring it via `monkeypatch`) lets these tests drive the real engine
# through engineered failure modes in milliseconds, fully isolated from
# real-repo content and real business-rule semantics.
# ---------------------------------------------------------------------------


def _fake_module(rules: list[Rule]) -> types.ModuleType:
    module = types.ModuleType("fake-group")
    module.RULES = tuple(rules)
    module.COLLECTORS = ()
    return module


def _fake_group_imports(
    rules_by_group: dict[str, list[Rule]],
    *,
    broken_groups: dict[str, str] | None = None,
) -> tuple[tuple[str, types.ModuleType | None, str | None], ...]:
    """Build a `_GROUP_IMPORTS`-shaped tuple covering the real six group names.

    `broken_groups` maps a group name to an import-error message, simulating
    that group's module failing to import; every other named group gets a
    trivial synthetic module built from `rules_by_group`.
    """
    broken = broken_groups or {}
    entries = []
    for name in runner.GROUP_MODULE_NAMES:
        if name in broken:
            entries.append((name, None, broken[name]))
        else:
            entries.append((name, _fake_module(rules_by_group.get(name, [])), None))
    return tuple(entries)


def _write_synthetic_repo(root: Path, guard_ids: list[str]) -> None:
    """Write the smallest valid repo+registry referencing exactly `guard_ids`.

    Each guard gets its own owner and its own single-file selector, so
    selectors never overlap and every selector always resolves.
    """
    for name in ("src", "scripts", "tests", ".apm"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    owners_dir = root / ".apm" / "architecture" / "owners"
    owners_dir.mkdir(parents=True, exist_ok=True)
    owners = []
    for index, guard in enumerate(guard_ids):
        (root / "src" / f"guard_{index}.py").write_text("x = 1\n", encoding="utf-8")
        owners.append(
            {
                "id": f"fixture-owner-{index}",
                "decision": f"Fixture decision {index}",
                "owner": f"core/guard_{index}.py (FixtureOwner{index})",
                "selectors": [f"src/guard_{index}.py"],
                "guards": [guard],
            }
        )
    (owners_dir / "index.json").write_text(
        json.dumps({"version": 1, "shards": ["fixture.json"]}), encoding="utf-8"
    )
    (owners_dir / "fixture.json").write_text(
        json.dumps({"version": 1, "owners": owners}), encoding="utf-8"
    )


def _run_synthetic(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    rules_by_group: dict[str, list[Rule]],
    *,
    broken_groups: dict[str, str] | None = None,
    selected_rule_ids: list[str] | None = None,
) -> RunReport:
    fake_imports = _fake_group_imports(rules_by_group, broken_groups=broken_groups)
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", fake_imports)
    if selected_rule_ids is None:
        return runner.run(root)
    return runner.run_selected_rules(root, selected_rule_ids)


def test_missing_group_import_error_is_isolated_from_other_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One group's ImportError does not stop the other five from running."""
    _write_synthetic_repo(tmp_path, ["keep-guard"])
    keep_rule = Rule(
        id="keep-rule",
        group="registry_delegation",
        guard_ids=("keep-guard",),
        description="d",
        check=lambda provider: [],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [keep_rule]},
        broken_groups={"contracts_tests": "boom import"},
    )

    assert report.violations == ()
    assert report.failures == (Failure(stage="import:contracts_tests", message="boom import"),)
    assert report.exit_code == 1
    by_group = {group.group: group for group in report.group_results}
    assert by_group["contracts_tests"].import_error == "boom import"
    assert by_group["contracts_tests"].rules == ()
    assert [(r.rule_id, r.error) for r in by_group["registry_delegation"].rules] == [
        ("keep-rule", None)
    ]
    for name in runner.GROUP_MODULE_NAMES:
        if name != "contracts_tests":
            assert by_group[name].import_error is None


@pytest.mark.parametrize(
    ("malformation", "diagnostic"),
    [
        ("rules-list", "RULES must be a tuple of Rule instances"),
        ("mismatched-group", "declares mismatched group"),
        ("collectors-string", "COLLECTORS must be a tuple of collector instances"),
    ],
)
def test_malformed_group_shapes_fail_closed_while_other_groups_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
    diagnostic: str,
) -> None:
    """Malformed group APIs cannot silently disable an unrelated good rule."""
    _write_synthetic_repo(tmp_path, ["keep-guard"])
    keep_rule = Rule(
        id="keep-rule",
        group="mutation_writes",
        guard_ids=("keep-guard",),
        description="d",
        check=lambda provider: [],
    )
    malformed = types.ModuleType("malformed-group")
    malformed.RULES = ()
    malformed.COLLECTORS = ()
    if malformation == "rules-list":
        malformed.RULES = []
    elif malformation == "mismatched-group":
        malformed.RULES = (Rule("bad-rule", "mutation_writes", (), "d", lambda provider: []),)
    else:
        malformed.COLLECTORS = "not-a-collector-tuple"
    imports = list(_fake_group_imports({"mutation_writes": [keep_rule]}))
    imports[0] = ("registry_delegation", malformed, None)
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", tuple(imports))

    report = runner.run(tmp_path)

    assert report.exit_code == 1
    assert any(diagnostic in failure.message for failure in report.failures)
    assert any(
        result.rule_id == "keep-rule" for group in report.group_results for result in group.rules
    )


def test_non_string_rule_metadata_is_structured_and_all_groups_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime type lies in one catalog cannot abort validation of sibling groups."""
    _write_synthetic_repo(tmp_path, ["keep-guard"])
    bad_id = Rule(17, "registry_delegation", (), "d", lambda provider: [])
    bad_group = Rule("bad-group", 23, (), "d", lambda provider: [])
    bad_description = Rule(
        "bad-description",
        "install_deployment",
        (),
        42,
        lambda provider: [],
    )
    keep_rule = Rule(
        id="keep-rule",
        group="mutation_writes",
        guard_ids=("keep-guard",),
        description="d",
        check=lambda provider: [Violation("keep-rule", "src/guard_0.py", 1, 1, "still ran")],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {
            "registry_delegation": [bad_id],
            "mutation_writes": [keep_rule],
            "contracts_tests": [bad_group],
            "install_deployment": [bad_description],
        },
    )

    assert report.exit_code == 1
    assert report.violations == (Violation("keep-rule", "src/guard_0.py", 1, 1, "still ran"),)
    failures = {(failure.stage, failure.message) for failure in report.failures}
    assert ("group:registry_delegation", "rule id must be a string: 17") in failures
    assert ("group:contracts_tests", "rule 'bad-group' group must be a string") in failures
    assert (
        "group:install_deployment",
        "rule 'bad-description' description must be a string",
    ) in failures


def test_runner_aggregates_inventory_failure_without_traceback(tmp_path: Path) -> None:
    """A missing root returns one structured fail-closed report."""
    report = runner.run(tmp_path / "does-not-exist")

    assert report.exit_code == 1
    assert report.violations == ()
    assert len(report.failures) == 1
    assert report.failures[0].stage == "inventory"
    assert "root is not a directory" in report.failures[0].message
    assert report.metrics.inventory_file_count == 0
    assert report.metrics.child_process_count == 0


def test_safe_missing_diagnostic_path_is_preserved_as_a_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rules may identify a required repository path that is currently absent."""
    _write_synthetic_repo(tmp_path, ["missing-path-guard"])
    missing_rule = Rule(
        id="missing-path-rule",
        group="registry_delegation",
        guard_ids=("missing-path-guard",),
        description="d",
        check=lambda provider: [
            Violation(
                rule_id="missing-path-rule",
                path="src/required_but_missing.py",
                line=1,
                column=1,
                message="required owner file is missing",
            )
        ],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [missing_rule]},
    )

    assert report.exit_code == 1
    assert report.violations == (
        Violation(
            rule_id="missing-path-rule",
            path="src/required_but_missing.py",
            line=1,
            column=1,
            message="required owner file is missing",
        ),
    )
    assert not any(failure.stage.startswith("rule-result:") for failure in report.failures)


def test_child_process_audit_is_measured_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process metric is an observed audit count, not a constant zero."""
    _write_synthetic_repo(tmp_path, ["process-guard"])

    def emit_process_event(_provider: object) -> list[Violation]:
        sys.audit("subprocess.Popen", "test-only")
        return []

    process_rule = Rule(
        id="process-rule",
        group="registry_delegation",
        guard_ids=("process-guard",),
        description="d",
        check=emit_process_event,
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [process_rule]},
    )

    assert report.exit_code == 1
    assert report.metrics.child_process_count == 1
    assert any(failure.stage == "process" for failure in report.failures)


def test_child_process_audit_counts_events_from_worker_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run-global audit count includes events emitted outside the caller context."""
    _write_synthetic_repo(tmp_path, ["thread-process-guard"])

    def emit_threaded_process_event(_provider: object) -> list[Violation]:
        worker = threading.Thread(target=lambda: sys.audit("subprocess.Popen", "test-only-worker"))
        worker.start()
        worker.join()
        return []

    process_rule = Rule(
        id="thread-process-rule",
        group="registry_delegation",
        guard_ids=("thread-process-guard",),
        description="d",
        check=emit_threaded_process_event,
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [process_rule]},
    )

    assert report.exit_code == 1
    assert report.metrics.child_process_count == 1
    assert any(failure.stage == "process" for failure in report.failures)


def test_child_process_audit_starts_before_group_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child-process event during the explicit import phase is measured."""
    _write_synthetic_repo(tmp_path, ["import-process-guard"])
    rule = Rule(
        id="import-process-rule",
        group="registry_delegation",
        guard_ids=("import-process-guard",),
        description="d",
        check=lambda provider: [],
    )
    imports = _fake_group_imports({"registry_delegation": [rule]})

    def audited_imports() -> tuple[tuple[str, types.ModuleType | None, str | None], ...]:
        sys.audit("subprocess.Popen", "test-only-import")
        return imports

    monkeypatch.setattr(runner, "_GROUP_IMPORTS", None)
    monkeypatch.setattr(runner, "_import_groups", audited_imports)

    report = runner.run(tmp_path)

    assert report.exit_code == 1
    assert report.metrics.child_process_count == 1
    assert any(failure.stage == "process" for failure in report.failures)


def test_rule_system_exit_is_structured_and_later_rules_still_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SystemExit(0) from a check is a failure, not process termination."""
    _write_synthetic_repo(tmp_path, ["exit-guard", "later-guard"])

    def exits(_provider: object) -> list[Violation]:
        raise SystemExit(0)

    rules = [
        Rule("exit-rule", "registry_delegation", ("exit-guard",), "d", exits),
        Rule(
            "later-rule",
            "registry_delegation",
            ("later-guard",),
            "d",
            lambda provider: [],
        ),
    ]

    report = _run_synthetic(monkeypatch, tmp_path, {"registry_delegation": rules})

    assert report.exit_code == 1
    assert any(
        failure.stage == "rule:exit-rule" and "raised: 0" in failure.message
        for failure in report.failures
    )
    results = next(
        group.rules for group in report.group_results if group.group == "registry_delegation"
    )
    assert [result.rule_id for result in results] == ["exit-rule", "later-rule"]
    assert results[1].error is None


def test_keyboard_interrupt_from_rule_is_not_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine operator interruption remains a process-level signal."""
    _write_synthetic_repo(tmp_path, ["interrupt-guard"])

    def interrupts(_provider: object) -> list[Violation]:
        raise KeyboardInterrupt

    rule = Rule(
        "interrupt-rule",
        "registry_delegation",
        ("interrupt-guard",),
        "d",
        interrupts,
    )
    monkeypatch.setattr(
        runner,
        "_GROUP_IMPORTS",
        _fake_group_imports({"registry_delegation": [rule]}),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(tmp_path)


def test_registry_traversal_shard_fails_before_any_shard_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsafe index shard cannot make SourceCache read outside the owners dir."""
    _write_synthetic_repo(tmp_path, ["safe-guard"])
    outside = tmp_path / "outside.json"
    outside.write_text('{"version": 1, "owners": []}', encoding="ascii")
    owners_dir = tmp_path / ".apm" / "architecture" / "owners"
    (owners_dir / "index.json").write_text(
        json.dumps({"version": 1, "shards": ["../../../outside.json"]}),
        encoding="ascii",
    )
    rule = Rule(
        "safe-rule",
        "registry_delegation",
        ("safe-guard",),
        "d",
        lambda provider: [],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [rule]},
    )

    assert report.exit_code == 1
    assert report.metrics.read_attempts == 1
    assert any(
        failure.stage == "registry" and "invalid registry shard name" in failure.message
        for failure in report.failures
    )


def test_registered_rules_raises_strictly_on_missing_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict test/tooling accessor raises rather than aggregating."""
    fake_imports = _fake_group_imports({}, broken_groups={"contracts_tests": "boom import"})
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", fake_imports)

    with pytest.raises(runner.RuleCatalogError, match=r"import:contracts_tests: boom import"):
        runner.registered_rules()


def test_duplicate_rule_id_across_groups_is_rejected_and_dropped_from_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule ID reused across groups is rejected; neither copy executes."""
    _write_synthetic_repo(tmp_path, ["clean-guard"])
    dup_a = Rule(
        id="dup-rule",
        group="registry_delegation",
        guard_ids=("dup-guard-a",),
        description="a",
        check=lambda provider: [],
    )
    dup_b = Rule(
        id="dup-rule",
        group="mutation_writes",
        guard_ids=("dup-guard-b",),
        description="b",
        check=lambda provider: [],
    )
    clean_rule = Rule(
        id="clean-rule",
        group="contracts_tests",
        guard_ids=("clean-guard",),
        description="c",
        check=lambda provider: [],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {
            "registry_delegation": [dup_a],
            "mutation_writes": [dup_b],
            "contracts_tests": [clean_rule],
        },
    )

    assert report.violations == ()
    assert report.failures == (
        Failure(stage="rules", message="duplicate rule id across groups: dup-rule"),
    )
    assert report.exit_code == 1
    by_group = {group.group: group for group in report.group_results}
    assert by_group["registry_delegation"].rules == ()
    assert by_group["mutation_writes"].rules == ()
    assert [(r.rule_id, r.error) for r in by_group["contracts_tests"].rules] == [
        ("clean-rule", None)
    ]


def test_registered_rules_raises_strictly_on_duplicate_rule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict accessor also rejects a cross-group duplicate rule ID."""
    dup_a = Rule(
        id="dup-rule",
        group="registry_delegation",
        guard_ids=("dup-guard-a",),
        description="a",
        check=lambda provider: [],
    )
    dup_b = Rule(
        id="dup-rule",
        group="mutation_writes",
        guard_ids=("dup-guard-b",),
        description="b",
        check=lambda provider: [],
    )
    fake_imports = _fake_group_imports({"registry_delegation": [dup_a], "mutation_writes": [dup_b]})
    monkeypatch.setattr(runner, "_GROUP_IMPORTS", fake_imports)

    with pytest.raises(runner.RuleCatalogError, match=r"duplicate rule id across groups: dup-rule"):
        runner.registered_rules()


def test_duplicate_guard_id_across_distinct_rules_skips_registry_but_rules_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard ID shared by two distinct rules disables registry validation
    and the guard-count invariant, but both rules still execute and their
    violations still aggregate."""
    _write_synthetic_repo(tmp_path, ["unrelated-placeholder-guard"])
    rule_a = Rule(
        id="rule-a",
        group="registry_delegation",
        guard_ids=("shared-guard",),
        description="a",
        check=lambda provider: [
            Violation(rule_id="rule-a", path="src/guard_0.py", line=1, column=1, message="from a")
        ],
    )
    rule_b = Rule(
        id="rule-b",
        group="mutation_writes",
        guard_ids=("shared-guard",),
        description="b",
        check=lambda provider: [
            Violation(rule_id="rule-b", path="src/guard_0.py", line=2, column=1, message="from b")
        ],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [rule_a], "mutation_writes": [rule_b]},
    )

    assert len(report.violations) == 2
    assert {v.rule_id for v in report.violations} == {"rule-a", "rule-b"}
    assert report.failures == (
        Failure(
            stage="registry",
            message="skipped: duplicate guard ids make owner-registry validation unsafe",
        ),
        Failure(stage="rules", message="duplicate guard id across rules: shared-guard"),
    )
    by_group = {group.group: group for group in report.group_results}
    assert by_group["registry_delegation"].rules[0].error is None
    assert by_group["mutation_writes"].rules[0].error is None


def test_unknown_selected_rule_id_fails_closed_and_skips_the_guard_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown selection is reported; unselected real rules do not run,
    and the whole-catalog guard invariant is skipped for a partial run."""
    _write_synthetic_repo(tmp_path, ["guard-x", "guard-y"])
    rule_x = Rule(
        id="rule-x",
        group="registry_delegation",
        guard_ids=("guard-x",),
        description="x",
        check=lambda provider: [],
    )
    rule_y = Rule(
        id="rule-y",
        group="mutation_writes",
        guard_ids=("guard-y",),
        description="y",
        check=lambda provider: [],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [rule_x], "mutation_writes": [rule_y]},
        selected_rule_ids=["totally-bogus-rule-id"],
    )

    assert report.violations == ()
    assert report.failures == (
        Failure(stage="selection", message="unknown selected rule IDs: totally-bogus-rule-id"),
    )
    by_group = {group.group: group for group in report.group_results}
    assert by_group["registry_delegation"].rules == ()
    assert by_group["mutation_writes"].rules == ()


def test_partial_valid_selection_runs_only_the_selected_rule_and_still_reports_unknowns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mix of one known and one unknown selected ID runs the known rule and
    still surfaces the unknown-ID failure in the same run."""
    _write_synthetic_repo(tmp_path, ["guard-x", "guard-y"])
    rule_x = Rule(
        id="rule-x",
        group="registry_delegation",
        guard_ids=("guard-x",),
        description="x",
        check=lambda provider: [
            Violation(rule_id="rule-x", path="src/guard_0.py", line=1, column=1, message="hit")
        ],
    )
    rule_y = Rule(
        id="rule-y",
        group="mutation_writes",
        guard_ids=("guard-y",),
        description="y",
        check=lambda provider: [],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [rule_x], "mutation_writes": [rule_y]},
        selected_rule_ids=["rule-x", "still-bogus"],
    )

    assert [v.rule_id for v in report.violations] == ["rule-x"]
    assert report.failures == (
        Failure(stage="selection", message="unknown selected rule IDs: still-bogus"),
    )
    by_group = {group.group: group for group in report.group_results}
    assert [r.rule_id for r in by_group["registry_delegation"].rules] == ["rule-x"]
    assert by_group["mutation_writes"].rules == ()


def test_every_malformed_result_category_fails_closed_while_later_rules_still_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seven distinct failure categories in one group all aggregate, and an
    eighth, later, well-behaved rule still runs and reports its finding.

    Also proves the guard-execution invariant's failure side: every one of
    the seven bad rules' guards is reported as not having executed exactly
    once, while the eighth rule's guard is not (it *did* execute once).
    """
    guard_ids = [f"guard-{letter}" for letter in "abcdefgh"]
    _write_synthetic_repo(tmp_path, guard_ids)

    def check_throws(_provider: object) -> list[Violation]:
        raise RuntimeError("boom-a")

    def check_malformed_noniterable(_provider: object) -> int:
        return 42  # not iterable

    def check_malformed_item(_provider: object) -> list[object]:
        return [object()]  # not a Violation

    def check_wrong_rule_id(_provider: object) -> list[Violation]:
        return [
            Violation(
                rule_id="not-d-wrong-id", path="src/guard_3.py", line=1, column=1, message="x"
            )
        ]

    def check_bad_location(_provider: object) -> list[Violation]:
        return [
            Violation(
                rule_id="e-bad-location", path="src/guard_4.py", line=0, column=1, message="x"
            )
        ]

    def check_non_ascii(_provider: object) -> list[Violation]:
        return [
            Violation(
                rule_id="f-non-ascii",
                path="src/guard_5.py",
                line=1,
                column=1,
                message="bad \N{SNOWMAN} message",
            )
        ]

    def check_bad_path(_provider: object) -> list[Violation]:
        return [Violation(rule_id="g-bad-path", path="../escape.py", line=1, column=1, message="x")]

    def check_good(_provider: object) -> list[Violation]:
        return [
            Violation(
                rule_id="h-good", path="src/guard_7.py", line=2, column=3, message="a real finding"
            )
        ]

    rules = [
        Rule("a-bad-throws", "registry_delegation", ("guard-a",), "a", check_throws),
        Rule(
            "b-bad-malformed",
            "registry_delegation",
            ("guard-b",),
            "b",
            check_malformed_noniterable,
        ),
        Rule("c-bad-item", "registry_delegation", ("guard-c",), "c", check_malformed_item),
        Rule("d-wrong-id", "registry_delegation", ("guard-d",), "d", check_wrong_rule_id),
        Rule("e-bad-location", "registry_delegation", ("guard-e",), "e", check_bad_location),
        Rule("f-non-ascii", "registry_delegation", ("guard-f",), "f", check_non_ascii),
        Rule("g-bad-path", "registry_delegation", ("guard-g",), "g", check_bad_path),
        Rule("h-good", "registry_delegation", ("guard-h",), "h", check_good),
    ]

    report = _run_synthetic(monkeypatch, tmp_path, {"registry_delegation": rules})

    assert report.exit_code == 1
    assert report.violations == (
        Violation(
            rule_id="h-good", path="src/guard_7.py", line=2, column=3, message="a real finding"
        ),
    )
    normalized = [
        (failure.stage, re.sub(r"0x[0-9a-fA-F]+", "0xADDR", failure.message))
        for failure in report.failures
    ]
    assert normalized == [
        ("guard", "owner guard did not execute exactly once: guard-a (count=0)"),
        ("guard", "owner guard did not execute exactly once: guard-b (count=0)"),
        ("guard", "owner guard did not execute exactly once: guard-c (count=0)"),
        ("guard", "owner guard did not execute exactly once: guard-d (count=0)"),
        ("guard", "owner guard did not execute exactly once: guard-e (count=0)"),
        ("guard", "owner guard did not execute exactly once: guard-f (count=0)"),
        ("guard", "owner guard did not execute exactly once: guard-g (count=0)"),
        ("rule-result:b-bad-malformed", "check() did not return an iterable"),
        (
            "rule-result:c-bad-item",
            "check() yielded a non-Violation item: <object object at 0xADDR>",
        ),
        (
            "rule-result:d-wrong-id",
            "violation rule_id 'not-d-wrong-id' does not match rule 'd-wrong-id'",
        ),
        ("rule-result:e-bad-location", "violation has non-positive line/column: 0:1"),
        (
            "rule-result:f-non-ascii",
            "violation message must be non-empty ASCII: 'bad \N{SNOWMAN} message'",
        ),
        (
            "rule-result:g-bad-path",
            "violation path must be a safe repository-relative POSIX path: '../escape.py'",
        ),
        ("rule:a-bad-throws", "raised: boom-a"),
    ]
    # No failure at all is reported for guard-h: the eighth rule executed
    # exactly once, which is the invariant's success path.
    assert not any("guard-h" in message for _stage, message in normalized)

    (registry_delegation_result,) = [
        group for group in report.group_results if group.group == "registry_delegation"
    ]
    executed_rule_ids = [rule_result.rule_id for rule_result in registry_delegation_result.rules]
    assert executed_rule_ids == [rule.id for rule in rules]


@pytest.mark.parametrize(
    "raw_result",
    [None, "", b"", bytearray(), memoryview(b""), {}, set(), frozenset()],
    ids=[
        "none",
        "string",
        "bytes",
        "bytearray",
        "memoryview",
        "mapping",
        "set",
        "frozenset",
    ],
)
def test_result_validator_rejects_invalid_empty_containers(raw_result: object) -> None:
    """Empty lookalike containers cannot masquerade as a clean rule result."""
    rule = Rule("safe-rule", "registry_delegation", (), "d", lambda provider: [])

    result = runner._validate_result(rule, raw_result)

    assert isinstance(result, str)
    assert result


def test_result_validator_converts_system_exit_during_iteration_to_failure() -> None:
    """A lazy result iterable cannot terminate the process while materializing."""

    class ExitingIterable:
        def __iter__(self) -> object:
            raise SystemExit(0)

    rule = Rule("safe-rule", "registry_delegation", (), "d", lambda provider: [])

    result = runner._validate_result(rule, ExitingIterable())

    assert result == "check() result iteration raised SystemExit: 0"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute.py",
        "./relative.py",
        "../escape.py",
        "src/../escape.py",
        "src\\windows.py",
        "src//double.py",
        "src/trailing/",
        " leading.py",
        "trailing.py ",
        "src/\N{SNOWMAN}.py",
    ],
)
def test_result_validator_rejects_unsafe_diagnostic_paths(path: str) -> None:
    """Every path-normalization escape fails the rule-result contract."""
    rule = Rule("safe-rule", "registry_delegation", (), "d", lambda provider: [])
    result = runner._validate_result(
        rule,
        [Violation("safe-rule", path, 1, 1, "message")],
    )

    assert isinstance(result, str)
    assert "safe repository-relative POSIX path" in result


def test_clean_synthetic_run_has_every_registered_guard_execute_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: two well-behaved rules each satisfy their own guard."""
    _write_synthetic_repo(tmp_path, ["fake-guard-one", "fake-guard-two"])
    rule_one = Rule(
        id="fake-rule-one",
        group="registry_delegation",
        guard_ids=("fake-guard-one",),
        description="d1",
        check=lambda provider: [],
    )
    rule_two = Rule(
        id="fake-rule-two",
        group="mutation_writes",
        guard_ids=("fake-guard-two",),
        description="d2",
        check=lambda provider: [],
    )

    report = _run_synthetic(
        monkeypatch,
        tmp_path,
        {"registry_delegation": [rule_one], "mutation_writes": [rule_two]},
    )

    assert report.violations == ()
    assert report.failures == ()
    assert report.exit_code == 0
    assert [group.group for group in report.group_results] == list(runner.GROUP_MODULE_NAMES)
    assert report.metrics.child_process_count == 0


# ---------------------------------------------------------------------------
# Section E: diagnostics -- deterministic ordering and strict ASCII format.
# ---------------------------------------------------------------------------

_DIAGNOSTIC_LINE = re.compile(r"^\S+:\d+:\d+: \S+ .+$")


def test_diagnostic_collector_sorts_violations_by_path_line_column_rule_id() -> None:
    """Insertion order never leaks into the collected violations."""
    collector = DiagnosticCollector()
    violations = [
        Violation(rule_id="z-rule", path="b/file.py", line=5, column=1, message="third"),
        Violation(rule_id="a-rule", path="a/file.py", line=2, column=9, message="second-b"),
        Violation(rule_id="a-rule", path="a/file.py", line=2, column=1, message="second-a"),
        Violation(rule_id="a-rule", path="a/file.py", line=1, column=1, message="first"),
    ]
    for violation in violations:
        collector.add_violation(violation)

    assert [v.message for v in collector.violations] == [
        "first",
        "second-a",
        "second-b",
        "third",
    ]


def test_diagnostic_collector_sorts_failures_by_stage_then_message() -> None:
    """Failures are ordered by (stage, message), independent of insertion order."""
    collector = DiagnosticCollector()
    failures = [
        Failure(stage="rule:z", message="z failure"),
        Failure(stage="import:a", message="second"),
        Failure(stage="import:a", message="first"),
    ]
    for failure in failures:
        collector.add_failure(failure)

    assert [(f.stage, f.message) for f in collector.failures] == [
        ("import:a", "first"),
        ("import:a", "second"),
        ("rule:z", "z failure"),
    ]


def test_render_violations_and_failures_is_order_independent_and_ascii() -> None:
    """Rendering the same findings in any input order gives the same string,
    violations block first, then failures, all strict printable ASCII."""
    violations = (
        Violation(rule_id="z-rule", path="b/file.py", line=5, column=1, message="third finding"),
        Violation(rule_id="a-rule", path="a/file.py", line=1, column=1, message="first finding"),
    )
    failures = (
        Failure(stage="rule:z", message="a failure"),
        Failure(stage="import:a", message="an earlier failure"),
    )

    rendered = render_violations_and_failures(violations, failures)
    rendered_from_shuffled = render_violations_and_failures(
        tuple(reversed(violations)), tuple(reversed(failures))
    )

    expected_lines = [
        format_violation(violations[1]),  # a/file.py sorts before b/file.py
        format_violation(violations[0]),
        format_failure(failures[1]),  # "import:a" sorts before "rule:z"
        format_failure(failures[0]),
    ]
    assert rendered == "\n".join(expected_lines)
    assert rendered_from_shuffled == rendered
    assert rendered.isascii()
    assert all(_DIAGNOSTIC_LINE.match(line) for line in rendered.splitlines())


def test_render_violations_and_failures_is_empty_when_nothing_was_collected() -> None:
    """A run with no findings renders no output at all."""
    assert render_violations_and_failures((), ()) == ""


def test_failure_renderer_escapes_unicode_and_control_characters() -> None:
    """Unexpected exception text cannot break the all-ASCII one-line contract."""
    rendered = format_failure(Failure(stage="read:\N{SNOWMAN}", message="bad\npath\t\N{SNOWMAN}"))

    assert rendered.isascii()
    assert rendered.count("\n") == 0
    assert "\\u2603" in rendered
    assert "bad\\npath\\t" in rendered


def test_format_violation_and_failure_shapes_match_path_line_column_rule_id_message() -> None:
    """Both finding kinds render as the same grep-able Ruff-style shape."""
    violation = Violation(
        rule_id="some-rule-id", path="src/pkg/mod.py", line=12, column=5, message="a finding"
    )
    failure = Failure(stage="registry", message="a startup failure")

    assert format_violation(violation) == "src/pkg/mod.py:12:5: some-rule-id a finding"
    assert format_failure(failure) == "registry:1:1: architecture-linter-failure a startup failure"
    assert _DIAGNOSTIC_LINE.match(format_violation(violation))
    assert _DIAGNOSTIC_LINE.match(format_failure(failure))


def test_format_violation_adds_display_only_stable_legacy_ac_aliases() -> None:
    """Historical AC labels follow, but never replace, the semantic rule ID."""
    violation = Violation(
        rule_id="transport-platform-git-cache-identity",
        path="src/apm_cli/deps/shared_clone_cache.py",
        line=42,
        column=9,
        message="cache identity drifted",
    )

    rendered = format_violation(violation)

    assert rendered == (
        "src/apm_cli/deps/shared_clone_cache.py:42:9: "
        "transport-platform-git-cache-identity [legacy AC11] cache identity drifted"
    )
    assert violation.rule_id == "transport-platform-git-cache-identity"
    assert rendered.isascii()
    assert set(LEGACY_AC_ALIASES) <= {rule.id for rule in runner.registered_rules()}


# ---------------------------------------------------------------------------
# Section F: the Python-only `selected_rule_ids` test API against the real
# registered rules. Selecting one real rule out of 103 keeps this fast (well
# under a second) -- it is not a full-catalog run.
# ---------------------------------------------------------------------------


def test_python_selected_rule_api_runs_only_the_selected_real_rule() -> None:
    """Selecting one real rule ID executes only that rule's group/rule."""
    picked = "registry_delegation.diagnostic_ascii_owner"
    assert picked in {rule.id for rule in runner.registered_rules()}

    report = runner.run_selected_rules(REAL_ROOT, [picked])

    executed_ids = [
        rule_result.rule_id for group in report.group_results for rule_result in group.rules
    ]
    assert executed_ids == [picked]
    assert report.failures == ()
    assert report.exit_code == 2


def test_public_run_has_no_partial_selection_surface() -> None:
    """The full-lint API cannot be turned into a successful partial lint."""
    picked = "registry_delegation.diagnostic_ascii_owner"

    with pytest.raises(TypeError, match="selected_rule_ids"):
        runner.run(REAL_ROOT, selected_rule_ids=[picked])  # type: ignore[call-arg]

    partial = runner.run_selected_rules(REAL_ROOT, [picked])
    executed = [result.rule_id for group in partial.group_results for result in group.rules]
    assert executed == [picked]
    assert partial.exit_code == 2
