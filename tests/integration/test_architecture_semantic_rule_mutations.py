"""Frozen mutation-break matrix for every guard-less architecture rule.

`tests/integration/test_architecture_owner_rule_mutations.py` proves that the
rules backing a canonical-owner guard have teeth.  It cannot say anything about
the other half of the catalog: the rules registered with `guard_ids == ()`.
Those rules defend decisions that have no entry in
`.apm/architecture/owners/*.json`, so nothing cross-checks them by name --
a semantic rule whose body was gutted still registers, still runs, still
reports a clean run, and still ships.

This file supplies the missing proof for that half.  For each of the 48
guard-less rules it pins one minimal, meaningful source mutation -- a surgical
edit that kills a load-bearing sub-condition of the decision the rule owns --
and asserts that exact rule reports a real `Violation`.  Coverage is a set
equality against the live registry, so a new guard-less rule that lands without
a mutation case fails here instead of shipping toothless.

Design notes:

* Mutations are applied through `run_selected_rules(..., source_overrides=...)`,
  which swaps the text a rule sees in memory.  Nothing is copied, nothing is
  written, and no repository sandbox is materialized.
* Exactly one rule executes per case, and only a `Violation` carrying that
  rule's ID counts.  A bare non-zero exit code, a startup failure, or a
  registry failure is explicitly rejected as proof of teeth.
* Mutations must stay surgical.  A syntax error, an unreadable file, or a
  truncated module would trip almost any rule for the wrong reason, so
  `test_semantic_rule_mutation_is_surgical_and_meaningful` rejects those shapes
  before the linter ever runs.
* Source fragments drift.  `_mutate` fails loudly when a pinned fragment is
  absent or ambiguous, so a rename in product code cannot silently turn a case
  into a no-op that still "passes".
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from scripts.architecture_linter.models import Rule
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = [
    pytest.mark.component,
    # The module-scoped baseline fixture below pays for one linter run across
    # every guard-less rule. `--dist loadgroup` (the xdist scheduler this
    # repo's sharded integration runs use) is the only scheduler that honors
    # `xdist_group`; without it these cases could be split across workers and
    # each worker would recompute that baseline.
    pytest.mark.xdist_group(name="architecture_semantic_rule_mutations"),
]

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MutationCase:
    """One guard-less rule's minimal source mutation.

    `old` is a literal fragment of the current file at `path`; `new` replaces
    it (the first occurrence, or every occurrence when `replace_all` is set).
    `intent` records which load-bearing sub-condition the edit kills, so a
    future reader can tell a real regression proof from an incidental edit.
    """

    rule_id: str
    path: str
    old: str
    new: str
    intent: str
    replace_all: bool = False


MUTATIONS: tuple[MutationCase, ...] = (
    MutationCase(
        rule_id="contracts-tests-executable-contract-authorities",
        path="tests/integration/conftest.py",
        old="def _resolve_apm_binary() -> Path | None:",
        new="def _resolve_apm_binary_impl() -> Path | None:",
        intent="Integration conftest stops owning the single apm binary resolver.",
    ),
    MutationCase(
        rule_id="contracts-tests-lifecycle-smoke-partition",
        path="tests/quality/test_ci_topology.py",
        old="assert merge_group < full,",
        new="assert merge_group <= full,",
        intent="Lifecycle partition stops requiring merge-group to be a strict subset.",
    ),
    MutationCase(
        rule_id="contracts-tooling-ado-lock-coordinates",
        path="src/apm_cli/deps/lockfile.py",
        old=").with_derived_provider_coordinates()",
        new=")",
        intent="Lock entries stop deriving ADO provider coordinates from the reference.",
    ),
    MutationCase(
        rule_id="install-deployment-approval-outcome-routing",
        path="src/apm_cli/policy/outcome_routing.py",
        old="POLICY_RESOLUTION_FAILURE_OUTCOMES = frozenset(",
        new="POLICY_RESOLUTION_FAILURE_OUTCOMES = set(",
        intent="Policy outcome routing stops owning the outcome set as a frozen constant.",
    ),
    MutationCase(
        rule_id="install-deployment-audit-policy-discovery",
        path="src/apm_cli/commands/audit.py",
        old="fetch_result = discover_policy_with_chain(cfg.project_root)",
        new="fetch_result = discover_policy(cfg.project_root)",
        intent="Audit resolves policy without the chain-aware discovery entry point.",
    ),
    MutationCase(
        rule_id="install-deployment-cached-claude-skill-metadata",
        path="src/apm_cli/models/validation.py",
        old='version="unknown",',
        new='version="0.0.0",',
        intent="Claude Skill validation invents a version instead of marking it unknown.",
    ),
    MutationCase(
        rule_id="install-deployment-dependency-winner-selection",
        path="src/apm_cli/deps/apm_resolver.py",
        old="ordered, winner_ids = _select_dependency_winners(",
        new="ordered, winner_ids = _select_flatten_winners(",
        intent="Dependency flattening stops sharing the one winner-selection helper.",
    ),
    MutationCase(
        rule_id="install-deployment-deployment-frame-projection",
        path="src/apm_cli/compilation/link_resolver.py",
        old="candidate_in_deployment = ctx.deployment_package_root / package_relative",
        new="candidate_in_deployment = source_package_root / package_relative",
        intent="UnifiedLinkResolver stops projecting assets into the deployment frame.",
    ),
    MutationCase(
        rule_id="install-deployment-git-object-field-authority",
        path="src/apm_cli/models/dependency/reference.py",
        old="reject_unknown_git_fields(entry, parent=True)",
        new="reject_unknown_git_fields(entry)",
        intent="Parent Git entries lose their object-form field admission check.",
    ),
    MutationCase(
        rule_id="install-deployment-gitlab-facade-orchestration",
        path="src/apm_cli/policy/discovery.py",
        old="result = _gitlab._fetch_from_gitlab_repo(",
        new="result = _gitlab._fetch_gitlab_contents(",
        intent="The policy facade reaches into GitLab transport internals directly.",
    ),
    MutationCase(
        rule_id="install-deployment-gitlab-policy-adapter",
        path="src/apm_cli/policy/_gitlab.py",
        old="def _fetch_gitlab_contents(",
        new="def _fetch_gitlab_contents_impl(",
        intent="The GitLab adapter drops one of its four owned fetch/state helpers.",
    ),
    MutationCase(
        rule_id="install-deployment-incomplete-chain-routing",
        path="src/apm_cli/policy/outcome_routing.py",
        old="incomplete_chain",
        new="chain_incomplete",
        intent="Fail-closed outcome routing renames the incomplete-chain outcome.",
        replace_all=True,
    ),
    MutationCase(
        rule_id="install-deployment-local-bundle-policy-preflight",
        path="src/apm_cli/install/local_bundle_handler.py",
        old="cache_only=True,",
        new="cache_only=False,",
        intent="Local bundle policy preflight stops running cache-only.",
    ),
    MutationCase(
        rule_id="install-deployment-local-identity-anchor",
        path="src/apm_cli/models/dependency/identity.py",
        old='        if anchored_local_path:\n            return f"local:{anchored_local_path}"',
        new='        if local_path:\n            return f"local:{local_path}"',
        intent="Local dependency identity ignores its declaring anchor.",
    ),
    MutationCase(
        rule_id="install-deployment-locked-skill-subset-reconstruction",
        path="src/apm_cli/deps/lockfile.py",
        old="skill_subset=sorted(self.skill_subset) if self.skill_subset else None,",
        new="skill_subset=None,",
        intent="Locked dependencies drop skill_subset when rebuilding their reference.",
    ),
    MutationCase(
        rule_id="install-deployment-manifest-inheritance-includes",
        path="src/apm_cli/policy/inheritance.py",
        old=(
            "        require_explicit_includes=parent.require_explicit_includes\n"
            "        or child.require_explicit_includes,\n"
        ),
        new="",
        intent="Manifest inheritance stops merging require_explicit_includes.",
    ),
    MutationCase(
        rule_id="install-deployment-marketplace-mutation-lock",
        path="src/apm_cli/marketplace/registry.py",
        old=(
            "    with _marketplace_mutation():\n"
            "        sources = [s for s in _load() if s.name.lower() != source.name.lower()]\n"
            "        sources.append(source)\n"
            "        _save(sources)"
        ),
        new=(
            "    sources = [s for s in _load() if s.name.lower() != source.name.lower()]\n"
            "    sources.append(source)\n"
            "    _save(sources)"
        ),
        intent="Marketplace registration load-modify-save escapes the mutation lock.",
    ),
    MutationCase(
        rule_id="install-deployment-plugin-bin-eligibility",
        path="src/apm_cli/install/exec_gate.py",
        old="def plugin_bin_deployable(",
        new="def plugin_bin_deployable_impl(",
        intent="The exec gate stops defining the one plugin bin eligibility owner.",
    ),
    MutationCase(
        rule_id="install-deployment-ref-recheck-ownership",
        path="src/apm_cli/install/phases/resolve.py",
        old="                if not should_force_ref_recheck(",
        new="                if not _should_recheck_ref(",
        intent="The resolve phase decides ref rechecks without the drift owner.",
    ),
    MutationCase(
        rule_id="install-deployment-registry-dependency-intent",
        path="src/apm_cli/marketplace/resolver.py",
        old='source="registry",',
        new='source="git",',
        intent="Registry-backed marketplace plugins lose their registry source intent.",
    ),
    MutationCase(
        rule_id="install-deployment-require-hashes-enforcement",
        path="src/apm_cli/install/pipeline.py",
        old="        _enforce_require_hashes(ctx)",
        new=(
            "        if ctx.policy.security.integrity.require_hashes:\n"
            "            _enforce_require_hashes(ctx)"
        ),
        intent="The install pipeline re-reads require_hashes from policy itself.",
    ),
    MutationCase(
        rule_id="install-deployment-resolver-queue-dedup",
        path="src/apm_cli/deps/apm_resolver.py",
        old="queued_keys.add(dep_ref.get_resolution_key())",
        new="queued_keys.add(dep_ref.get_unique_key())",
        intent="Resolver queue dedup keys on identity, discarding ref constraints.",
    ),
    MutationCase(
        rule_id="install-deployment-skill-subset-tokens",
        path="src/apm_cli/bundle/plugin_exporter.py",
        old="skill_subset = skill_subset_filter_tokens(dep.skill_subset)",
        new="skill_subset = set(dep.skill_subset)",
        intent="The plugin exporter builds subset filter tokens locally.",
    ),
    MutationCase(
        rule_id="install-deployment-update-plan-ref-annotation",
        path="src/apm_cli/install/helpers/ref_reuse.py",
        old="resolved = downloader.resolve_git_reference(dep_ref)",
        new='resolved = getattr(dep_ref, "ref", None)',
        intent="Update planning annotates refs without asking the downloader owner.",
    ),
    MutationCase(
        rule_id="marketplace-integrations-bundle-format-authority",
        path="src/apm_cli/bundle/reproducible_archive.py",
        old="shutil.copyfileobj(source, member)",
        new="member.write(source.read())",
        intent="Reproducible archives buffer a whole member instead of streaming it.",
    ),
    MutationCase(
        rule_id="marketplace-integrations-generated-bundle-lf-writers",
        path="src/apm_cli/bundle/packer.py",
        old='write_text_lf(bundle_dir / "apm.lock.yaml", enriched_yaml)',
        new='(bundle_dir / "apm.lock.yaml").write_text(enriched_yaml)',
        intent="Bundle lockfile metadata bypasses the deterministic LF writer.",
    ),
    MutationCase(
        rule_id="marketplace-integrations-hash-visible-lf-writers",
        path="src/apm_cli/deps/plugin_parser.py",
        old="write_text_lf(apm_yml_path, apm_yml_content)",
        new="apm_yml_path.write_text(apm_yml_content)",
        intent="A hash-visible synthesized manifest bypasses the canonical LF writer.",
    ),
    MutationCase(
        rule_id="marketplace-integrations-local-audit-resolution",
        path="src/apm_cli/marketplace/audit.py",
        old="local_plugin_path = resolve_local_plugin_path(",
        new="local_plugin_path = _resolve_local_relative_source(",
        intent="Local marketplace audit revives the retired relative-source resolver.",
    ),
    MutationCase(
        rule_id="marketplace-integrations-projection-boundary",
        path="src/apm_cli/install/services.py",
        old=(
            "    enforce_agent_plugin_deployment_boundary(package_info)\n"
            "\n"
            "    from apm_cli.integration.dispatch import get_dispatch_table\n"
        ),
        new=(
            "    from apm_cli.integration.dispatch import get_dispatch_table\n"
            "\n"
            "    enforce_agent_plugin_deployment_boundary(package_info)\n"
        ),
        intent="The native deployment boundary stops being the first integration action.",
    ),
    MutationCase(
        rule_id="marketplace-integrations-removed-plugin-lifecycle",
        path="src/apm_cli/bundle/local_bundle.py",
        old="bundle_root",
        new="data_root",
        intent="Local bundles revive the removed native plugin lifecycle vocabulary.",
        replace_all=True,
    ),
    MutationCase(
        rule_id="marketplace-integrations-source-parsing",
        path="src/apm_cli/marketplace/resolver.py",
        old="dependency = DependencyReference.parse_from_dict(entry)",
        new="dependency = DependencyReference.parse_from_mapping(entry)",
        intent="Packed marketplace sources stop parsing through DependencyReference.",
    ),
    MutationCase(
        rule_id="mutation_writes.drift_hook_membership",
        path="src/apm_cli/install/manifest_reconcile.py",
        old="paths = merge_hook_config_projection_specs(targets)",
        new="paths = merge_hook_config_projection_specs(tuple(targets))",
        intent="Merge-hook config paths stop deriving membership from projection specs.",
    ),
    MutationCase(
        rule_id="mutation_writes.hook_cleanup_scope",
        path="src/apm_cli/commands/prune.py",
        old="HookIntegrator().reconcile_after_removal(",
        new="HookIntegrator().reconcile_dropped_targets(",
        intent="Prune reaches into target-contraction hook cleanup.",
    ),
    MutationCase(
        rule_id="registry_delegation.agents_source_attribution",
        path="src/apm_cli/compilation/distributed_compiler.py",
        old='source_attribution = config.get("source_attribution", True)',
        new='source_attribution = config.get("attribution", True)',
        intent="The distributed compiler reads a non-canonical attribution config key.",
    ),
    MutationCase(
        rule_id="registry_delegation.command_machine_output",
        path="src/apm_cli/commands/policy.py",
        old='    """Emit the report as a single JSON object on stdout."""\n',
        new=(
            '    """Emit the report as a single JSON object on stdout."""\n'
            "    from ..utils.console import set_console_stderr\n"
            "\n"
            "    set_console_stderr(True)\n"
        ),
        intent="A command routes machine output itself instead of the root CLI.",
    ),
    MutationCase(
        rule_id="registry_delegation.diagnostic_ascii_owner",
        path="src/apm_cli/integration/agent_integrator.py",
        old='f"Codex agent {printable_ascii_text(source.name)}: {issue}. "',
        new='f"Codex agent {source.name}: {issue}. "',
        intent="A Codex diagnostic renders a raw agent name without ASCII normalization.",
    ),
    MutationCase(
        rule_id="registry_delegation.experimental_target_hints",
        path="src/apm_cli/install/target_hints.py",
        old="def emit_disabled_experimental_target_hint(",
        new="def emit_disabled_experimental_target_hint_impl(",
        intent="The target-hints owner stops defining the experimental hint emitter.",
    ),
    MutationCase(
        rule_id="registry_delegation.host_backend_dispatch",
        path="src/apm_cli/deps/host_backends.py",
        old='register_host_backend("gitlab", GitLabBackend)',
        new='_BACKEND_BY_KIND = {"gitlab": GitLabBackend}',
        intent="Host backends grow a parallel kind-to-backend dispatch table.",
    ),
    MutationCase(
        rule_id="registry_delegation.lifecycle_docs_aggregate",
        path="docs/src/content/docs/concepts/lifecycle.md",
        old="deploys individual primitives but does not run aggregate",
        new="deploys individual primitives and also runs aggregate",
        intent="Lifecycle docs stop stating that install skips aggregate compilation.",
    ),
    MutationCase(
        rule_id="registry_delegation.lockfile_version_authority",
        path="src/apm_cli/bundle/local_bundle.py",
        old="from ..deps.lockfile import require_supported_lockfile_version",
        new=(
            "from ..deps.lockfile import SUPPORTED_LOCKFILE_VERSIONS,"
            " require_supported_lockfile_version"
        ),
        intent="Local bundles import the supported-version vocabulary instead of the check.",
    ),
    MutationCase(
        rule_id="registry_delegation.logger_redaction_attachment",
        path="src/apm_cli/cli.py",
        old="handler.addFilter(SecretRedactionFilter())",
        new="apm_logger.addFilter(SecretRedactionFilter())",
        intent="Secret redaction attaches to the package logger instead of handlers.",
    ),
    MutationCase(
        rule_id="registry_delegation.manifest_schema_negotiation",
        path="src/apm_cli/models/apm_package.py",
        old="manifest_contract = negotiate_manifest_contract(data)",
        new='manifest_contract = negotiate_manifest_contract(data.get("$schema"))',
        intent="Manifest loading negotiates the schema identity outside its owner.",
    ),
    MutationCase(
        rule_id="registry_delegation.native_locator_target_names",
        path="src/apm_cli/install/deployed_paths.py",
        old="            if deploy_root is not None:",
        new='            if deploy_root is not None and _t.name == "copilot-app":',
        intent="Deployed-path translation branches on a native locator target name.",
    ),
    MutationCase(
        rule_id="registry_delegation.policy_ref_redaction",
        path="src/apm_cli/policy/discovery.py",
        old='"repo_ref": _redact_policy_ref(repo_ref),',
        new='"repo_ref": repo_ref,',
        intent="Policy cache metadata persists an unredacted repository ref.",
    ),
    MutationCase(
        rule_id="registry_delegation.root_cli_output_mode",
        path="src/apm_cli/cli.py",
        old="handler.addFilter(SecretRedactionFilter())",
        new="handler.filters.append(SecretRedactionFilter())",
        intent="The root CLI stops attaching redaction through the handler filter API.",
    ),
    MutationCase(
        rule_id="transport-platform-runtime-deadline-safety",
        path="src/apm_cli/runtime/base.py",
        old="time.monotonic",
        new="time.perf_counter",
        intent="Runtime streaming stops enforcing a monotonic wall-clock deadline.",
        replace_all=True,
    ),
    MutationCase(
        rule_id="transport-platform-tls-trust-injection",
        path="src/apm_cli/cli.py",
        old="from apm_cli.core.tls_trust import configure_process_tls_trust, log_tls_trust_status",
        new=(
            "import truststore\n"
            "from apm_cli.core.tls_trust import configure_process_tls_trust,"
            " log_tls_trust_status\n"
            "\n"
            "truststore.inject_into_ssl()"
        ),
        intent="The root CLI injects TLS trust outside the two sanctioned modules.",
    ),
)

CASE_IDS: tuple[str, ...] = tuple(case.rule_id for case in MUTATIONS)

# One case's mutated span may legitimately cover a few lines (a reordered gate,
# a dedented transaction body), but never a module.
MAX_FRAGMENT_LINES = 5
MAX_REMOVED_LINES = 2


@cache
def _source(path: str) -> str:
    """Return the current on-disk text of `path`, read at most once per session."""
    return (ROOT / path).read_text(encoding="utf-8")


@cache
def _rules_by_id() -> dict[str, Rule]:
    """Map every registered rule ID to its rule, validated by the runner."""
    return {rule.id: rule for rule in registered_rules()}


@cache
def _guardless_rule_ids() -> frozenset[str]:
    """Return every registered rule that defends no canonical-owner guard."""
    return frozenset(rule.id for rule in _rules_by_id().values() if not rule.guard_ids)


def _mutate(case: MutationCase) -> str:
    """Return `case`'s mutated source, proving its fragment still exists verbatim."""
    original = _source(case.path)
    occurrences = original.count(case.old)
    assert occurrences >= 1, (
        f"{case.rule_id}: fragment absent from {case.path}; the file drifted and this "
        f"mutation no longer tests anything: {case.old!r}"
    )
    if not case.replace_all:
        assert occurrences == 1, (
            f"{case.rule_id}: fragment is ambiguous in {case.path} "
            f"({occurrences} occurrences); pin a unique fragment or set replace_all"
        )
    return original.replace(case.old, case.new, -1 if case.replace_all else 1)


@pytest.fixture(scope="module")
def baseline_violated_rule_ids() -> frozenset[str]:
    """Run every guard-less rule once, unmutated, so each case can prove attribution."""
    report = run_selected_rules(ROOT, tuple(sorted(CASE_IDS)))
    return frozenset(violation.rule_id for violation in report.violations)


def test_matrix_covers_every_guardless_rule_exactly_once() -> None:
    """The matrix rule set must equal the guard-less catalog, one case each."""
    matrix_rule_ids = list(CASE_IDS)
    duplicates = sorted({rid for rid in matrix_rule_ids if matrix_rule_ids.count(rid) > 1})

    assert not duplicates, f"rules covered by more than one mutation case: {duplicates}"
    assert frozenset(matrix_rule_ids) == _guardless_rule_ids()
    assert len(MUTATIONS) == len(_guardless_rule_ids())


def test_matrix_case_order_is_deterministic() -> None:
    """Cases stay sorted by rule ID so parameterized IDs never reshuffle."""
    assert list(CASE_IDS) == sorted(CASE_IDS)


@pytest.mark.parametrize("case", MUTATIONS, ids=CASE_IDS)
def test_case_targets_a_registered_rule_that_owns_no_guard(case: MutationCase) -> None:
    """Each case must name a registered rule that declares no owner guard.

    A rule that later acquires a guard ID belongs to the owner matrix instead,
    and must be moved rather than covered twice.
    """
    rule = _rules_by_id().get(case.rule_id)

    assert rule is not None, f"{case.rule_id}: not a registered rule"
    assert rule.guard_ids == (), (
        f"{case.rule_id}: now declares guards {rule.guard_ids}; move this case to the "
        "owner-guard mutation matrix"
    )


@pytest.mark.parametrize("case", MUTATIONS, ids=CASE_IDS)
def test_semantic_rule_mutation_is_surgical_and_meaningful(case: MutationCase) -> None:
    """Mutations must edit real semantics, not break reading or parsing.

    A syntax error, an unreadable file, or a truncated module would trip most
    rules for reasons that have nothing to do with the guarded decision, so
    those shapes are rejected before the mutation is ever linted.
    """
    original = _source(case.path)
    mutated = _mutate(case)
    removed_lines = len(original.splitlines()) - len(mutated.splitlines())

    assert case.old.isascii() and case.new.isascii()
    assert case.intent.isascii() and case.intent.endswith(".")
    assert mutated != original
    assert case.old != original, f"{case.rule_id}: mutation replaces all of {case.path}"
    assert mutated.strip(), f"{case.rule_id}: mutation emptied {case.path}"
    assert case.old.count("\n") + 1 <= MAX_FRAGMENT_LINES
    assert case.new.count("\n") + 1 <= MAX_FRAGMENT_LINES
    assert removed_lines <= MAX_REMOVED_LINES, (
        f"{case.rule_id}: mutation deleted {removed_lines} lines of {case.path}; "
        "semantic rules must be broken surgically, not by truncation"
    )
    if case.path.endswith(".py"):
        ast.parse(mutated, filename=case.path)


def test_guardless_rules_report_nothing_before_mutation(
    baseline_violated_rule_ids: frozenset[str],
) -> None:
    """Every guard-less rule is clean at HEAD, so any violation below is the mutation."""
    assert baseline_violated_rule_ids == frozenset()


@pytest.mark.parametrize("case", MUTATIONS, ids=CASE_IDS)
def test_semantic_rule_catches_its_mutation(
    case: MutationCase, baseline_violated_rule_ids: frozenset[str]
) -> None:
    """Each guard-less rule must report a Violation for its own mutation.

    Only that one rule executes, and only a `Violation` carrying its ID counts:
    a non-zero exit code from unrelated startup, registry, or read failures is
    explicitly not accepted as proof that the rule has teeth.
    """
    assert case.rule_id not in baseline_violated_rule_ids, (
        f"{case.rule_id}: already violates unmutated"
    )

    report = run_selected_rules(ROOT, (case.rule_id,), source_overrides={case.path: _mutate(case)})

    blamed = tuple(item for item in report.violations if item.rule_id == case.rule_id)
    broken_input = tuple(
        failure.stage
        for failure in report.failures
        if failure.stage in (f"read:{case.path}", f"parse:{case.path}")
    )
    assert not broken_input, (
        f"{case.rule_id}: mutation made {case.path} unreadable or unparsable {broken_input}"
    )
    assert blamed, (
        f"{case.rule_id}: stayed silent after mutating {case.path} ({case.intent}) -- "
        f"the rule has no teeth for this decision. "
        f"failures={[(f.stage, f.message) for f in report.failures]}"
    )
    assert {item.rule_id for item in report.violations} == {case.rule_id}
