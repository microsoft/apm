"""Install entry routing and commit order analyzers (#4085).

``install-entry-routing``: every package install, and its ``--dry-run``
preview, enters ``run_install_pipeline`` through a closed list of callers;
``install --mcp`` reconciles its server through the install's MCP service
stage (``run_service_integrations``).

``install-commit-order``: every check completes before ``apm.lock.yaml`` and
deployed files are committed. The commit gate runs before the first
integrator write and on the staged lockfile before its writer scope commits
it (#4086); merged MCP policy is enforced
from a closed list of sites; the frozen pre-deploy check runs before the
pipeline; ``install --mcp`` judges policy through the commit gate;
``InstallTransaction`` has one definition. While the install-time
audit can block after deploy, only the transaction snapshots the deploy
targets: before the integrate phase, released only after the audit, and
restored by its rollback. Every rollback also removes the target roots the
attempt created and left empty (#4159).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _facts_for,
    _first_line_re,
    _lines,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_ENTRY_ROUTING = "install-deployment-install-entry-routing"
GUARD_COMMIT_ORDER = "install-deployment-install-commit-order"

_SERVICE = "src/apm_cli/install/service.py"
_COMMAND = "src/apm_cli/commands/install.py"
_PIPELINE = "src/apm_cli/install/pipeline.py"
_INTEGRATE = "src/apm_cli/install/phases/integrate.py"
_ENTRY_OWNER = "src/apm_cli/install/pipeline_entry.py"
_COMMIT_GATE = "src/apm_cli/install/commit_gate.py"
_TRANSACTION = "src/apm_cli/install/transaction.py"

# Closed list: the only modules that may enter the install pipeline.
_PIPELINE_CALLERS = frozenset({_SERVICE})
_ENTRY_CALLERS = frozenset(
    {
        _COMMAND,
        "src/apm_cli/commands/update.py",
        "src/apm_cli/commands/lock.py",
        "src/apm_cli/commands/deps/cli.py",
    }
)
_MCP_COMMAND = "src/apm_cli/install/mcp/command.py"
_MCP_INTEGRATION = "src/apm_cli/install/mcp/integration.py"
_SERVICE_INTEGRATION = "src/apm_cli/install/service_integration.py"
# Closed, shrink-only list of org-policy preflight sites outside the policy
# package. Bundle admission leaves it when bundles route through the pipeline.
_POLICY_PREFLIGHT_SITES = frozenset(
    {
        _ENTRY_OWNER,
        _COMMIT_GATE,
        "src/apm_cli/install/bundle_admission.py",
    }
)
# install --mcp judges its server through the commit gate and refuses it as
# apm install does.
_MCP_ENTRY_GATE_CALLS = ("enforce_mcp_policy(", "refuse_blocked_mcp(logger)")

# install --mcp reaches the install's MCP service stage, which records the
# entry in the lockfile; the command keeps no integrator or lockfile writes.
_MCP_ENTRY_ROUTE = (
    (_MCP_COMMAND, "run_service_integrations("),
    (_SERVICE_INTEGRATION, 'mcp_entry=getattr(ctx, "mcp_entry", None),'),
    (_MCP_INTEGRATION, "mcp_count = integrate_mcp_entry("),
)
_MCP_ENTRY_BYPASS = re.compile(r"\bMCPIntegrator\.(install|update_lockfile|remove_stale)\(")

_PIPELINE_CALL = re.compile(r"\brun_install_pipeline\(")
_ENTRY_CALL = re.compile(r"\b_install_apm_dependencies\(")
_POLICY_PREFLIGHT = re.compile(r"\brun_policy_preflight\b")
_TRANSACTION_CLASS = re.compile(r"^class InstallTransaction\b")
_SNAPSHOT_CAPTURE = re.compile(r"\bcapture_audit_gated_snapshot\(")


def _source_lines(provider: FactsProvider, path: str) -> tuple[str, ...]:
    """Return raw source lines without parsing: closed-list scans stay cheap."""
    text, _error = provider.source_cache.read(path)
    return tuple(text.splitlines()) if text else ()


def _code_lines(lines: tuple[str, ...]) -> list[tuple[int, str]]:
    """Return numbered lines with comments and docstring-style text dropped."""
    numbered = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith(("#", '"""', "``")) or "``" in stripped:
            continue
        numbered.append((number, line))
    return numbered


def _closed_list_violations(
    provider: FactsProvider,
    rule_id: str,
    pattern: re.Pattern[str],
    allowed: frozenset[str],
    message: str,
    *,
    skip_def: str | None = None,
) -> list[Violation]:
    found: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        if path in allowed or path.startswith("src/apm_cli/policy/"):
            continue
        for number, line in _code_lines(_source_lines(provider, path)):
            if skip_def and f"def {skip_def}" in line:
                continue
            if pattern.search(line):
                found.append(violation(rule_id, path, message, line=number))
    return found


def check_install_entry_routing(provider: FactsProvider) -> tuple[Violation, ...]:
    """Package installs and previews enter the pipeline from a closed caller list."""
    rule_id = GUARD_ENTRY_ROUTING
    command, failures = _facts_for(provider, _COMMAND, rule_id)
    pipeline, pipeline_failures = _facts_for(provider, _PIPELINE, rule_id)
    owner, owner_failures = _facts_for(provider, _ENTRY_OWNER, rule_id)
    if failures or pipeline_failures or owner_failures:
        return tuple(failures) + tuple(pipeline_failures) + tuple(owner_failures)
    found = _closed_list_violations(
        provider,
        rule_id,
        _PIPELINE_CALL,
        _PIPELINE_CALLERS,
        "run_install_pipeline is entered only through InstallService",
        skip_def="run_install_pipeline",
    )
    found += _closed_list_violations(
        provider,
        rule_id,
        _ENTRY_CALL,
        _ENTRY_CALLERS,
        "_install_apm_dependencies callers form a closed list (install entry routing)",
        skip_def="_install_apm_dependencies",
    )
    command_text = "\n".join(_lines(command))
    pipeline_text = "\n".join(_lines(pipeline))
    if not (
        "preview = pipeline_preview(" in command_text
        and "dry_run=True," in command_text
        and "preview=preview," in command_text
    ):
        found.append(
            _summary(rule_id, _COMMAND, "install --dry-run must preview through the pipeline")
        )
    owner_text = "\n".join(_lines(owner))
    if not ("def pipeline_preview(" in owner_text and "class PipelinePreview:" in owner_text):
        found.append(_summary(rule_id, _ENTRY_OWNER, "the dry-run preview entry owner is missing"))
    if "return _preview_install(ctx, run_gates=not refused)" not in pipeline_text:
        found.append(
            _summary(rule_id, _PIPELINE, "the pipeline must stop --dry-run before any write")
        )
    found += _mcp_entry_route_violations(provider, rule_id)
    return tuple(found)


def _mcp_entry_route_violations(provider: FactsProvider, rule_id: str) -> list[Violation]:
    """install --mcp reconciles through the install's MCP service stage."""
    found: list[Violation] = []
    for path, needle in _MCP_ENTRY_ROUTE:
        code = [text for _number, text in _code_lines(_source_lines(provider, path))]
        if not any(needle in text for text in code):
            found.append(
                _summary(rule_id, path, "install --mcp must reach the install's MCP service stage")
            )
    found.extend(
        violation(
            rule_id,
            _MCP_COMMAND,
            "install --mcp must not install or record MCP servers outside the service stage",
            line=number,
        )
        for number, text in _code_lines(_source_lines(provider, _MCP_COMMAND))
        if _MCP_ENTRY_BYPASS.search(text)
    )
    return found


def _ordered(facts: object, first: str, then: str) -> bool:
    """Return whether the first line containing `first` precedes `then`."""
    first_line = _first_line_re(facts, re.compile(re.escape(first)))
    then_line = _first_line_re(facts, re.compile(re.escape(then)))
    return first_line is not None and then_line is not None and first_line < then_line


def _deploy_snapshot_violations(provider: FactsProvider, loaded: dict) -> list[Violation]:
    """The audit-gated deploy snapshot is taken and restored only by the transaction."""
    rule_id = GUARD_COMMIT_ORDER
    pipeline = loaded[_PIPELINE]
    found: list[Violation] = []
    if not (
        _ordered(
            pipeline,
            "transaction.snapshot_deploy_targets(ctx)",
            "materialization_session = _run_integration_phase(ctx)",
        )
        and _ordered(pipeline, '_run_phase("audit"', "transaction.release_deploy_targets()")
        and _ordered(
            pipeline, "transaction.release_deploy_targets()", '_run_phase("copilot_plugins"'
        )
        and _ordered(
            pipeline,
            "transaction.track_created_target_roots(ctx.created_target_roots)",
            '_run_phase("targets"',
        )
    ):
        found.append(
            _summary(
                rule_id,
                _PIPELINE,
                "the transaction tracks created target roots before the targets phase, "
                "snapshots deploy targets before the integrate phase "
                "and releases them only after the install-time audit",
            )
        )
    transaction_text = "\n".join(_lines(loaded[_TRANSACTION]))
    if not all(
        needle in transaction_text
        for needle in (
            "self._deploy_snapshot = capture_audit_gated_snapshot(ctx)",
            "self._restore_deploy_targets()",
            "self._remove_created_target_roots()",
        )
    ):
        found.append(
            _summary(rule_id, _TRANSACTION, "InstallTransaction owns the deploy snapshot rollback")
        )
    found += _closed_list_violations(
        provider,
        rule_id,
        _SNAPSHOT_CAPTURE,
        frozenset({_TRANSACTION}),
        "only InstallTransaction captures the audit-gated deploy snapshot",
        skip_def="capture_audit_gated_snapshot",
    )
    return found


def check_install_commit_order(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every install check completes before the lockfile and deployed files commit."""
    rule_id = GUARD_COMMIT_ORDER
    paths = (_INTEGRATE, _PIPELINE, _SERVICE, _COMMIT_GATE, _TRANSACTION, _MCP_COMMAND)
    loaded = {}
    failures: list[Violation] = []
    for path in paths:
        facts, path_failures = _facts_for(provider, path, rule_id)
        loaded[path] = facts
        failures.extend(path_failures)
    if failures:
        return tuple(failures)
    found: list[Violation] = []
    if not _ordered(loaded[_INTEGRATE], "check_before_deploy(ctx)", "run_integration_template("):
        found.append(
            _summary(rule_id, _INTEGRATE, "check_before_deploy must run before any deploy")
        )
    if not (
        _ordered(
            loaded[_PIPELINE],
            "LockfileBuilder(ctx).build_and_save()",
            "check_before_lockfile(ctx, LockFile.read(",
        )
        and _ordered(
            loaded[_PIPELINE],
            "check_before_lockfile(ctx, LockFile.read(",
            '_run_phase("claude_plugins"',
        )
    ):
        found.append(
            _summary(
                rule_id, _PIPELINE, "check_before_lockfile must run before the lockfile commits"
            )
        )
    if not _ordered(
        loaded[_SERVICE],
        "self._enforce_frozen_deployed_hashes(request, deployed=False)",
        "self._run_pipeline(request)",
    ):
        found.append(
            _summary(rule_id, _SERVICE, "the frozen pre-deploy check must run before the pipeline")
        )
    found += _deploy_snapshot_violations(provider, loaded)
    gate_text = "\n".join(_lines(loaded[_COMMIT_GATE]))
    if not all(
        needle in gate_text
        for needle in (
            "def check_before_deploy(",
            "def check_before_lockfile(",
            "enforce_installed_hash_policy(ctx, ctx.existing_lockfile if frozen else prospective)",
            # Acquired package types decide which MCP declarations are checked.
            "dependency.package_type = ctx.package_types[key]",
            "enforce_mcp_policy(",
            "def refuse_blocked_mcp(",
        )
    ):
        found.append(_summary(rule_id, _COMMIT_GATE, "the commit gate owns every pre-commit check"))
    mcp_text = "\n".join(text for _number, text in _code_lines(_lines(loaded[_MCP_COMMAND])))
    if not all(call in mcp_text for call in _MCP_ENTRY_GATE_CALLS):
        found.append(
            _summary(
                rule_id, _MCP_COMMAND, "install --mcp must judge policy through the commit gate"
            )
        )
    found += _closed_list_violations(
        provider,
        rule_id,
        _POLICY_PREFLIGHT,
        _POLICY_PREFLIGHT_SITES,
        "org-policy preflight sites form a closed list (install commit order)",
    )
    for path in _python_paths(provider, _SRC_PREFIX):
        if path == _TRANSACTION:
            continue
        line = next(
            (
                number
                for number, text in enumerate(_source_lines(provider, path), start=1)
                if _TRANSACTION_CLASS.search(text)
            ),
            None,
        )
        if line is not None:
            found.append(
                violation(rule_id, path, "InstallTransaction has one definition", line=line)
            )
    if _first_line_re(loaded[_TRANSACTION], _TRANSACTION_CLASS) is None:
        found.append(_summary(rule_id, _TRANSACTION, "InstallTransaction owner is missing"))
    return tuple(found)


__all__ = [
    "GUARD_COMMIT_ORDER",
    "GUARD_ENTRY_ROUTING",
    "check_install_commit_order",
    "check_install_entry_routing",
]
