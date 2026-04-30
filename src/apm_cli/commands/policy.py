"""APM policy command group.

Diagnostic surface for the policy enforcement layer.  ``apm policy status``
lets admins and developers verify discovery, cache freshness, inheritance
chain, and the count of effective rules without running a full install or
audit.

The command is **always exit 0** -- failure to fetch is reported in the
output (machine or rendered), never via process exit, so it remains safe
for CI / SIEM ingestion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401, UP035

import click

from ..core.command_logger import CommandLogger
from ..policy._help_text import POLICY_SOURCE_FORMS_HELP
from ..policy.discovery import (
    DEFAULT_CACHE_TTL,  # noqa: F401
    MAX_STALE_TTL,
    PolicyFetchResult,
    _read_cache_entry,
    discover_policy,
    discover_policy_with_chain,
)
from ..policy.schema import ApmPolicy
from ..utils.console import (
    RICH_AVAILABLE,
    _get_console,
    _rich_echo,  # noqa: F401
    _rich_error,
    _rich_panel,
)

try:
    from rich.table import Table
except ImportError:  # pragma: no cover - Rich is a hard dep but stay defensive
    Table = None  # type: ignore[assignment]


# -- Helpers --------------------------------------------------------


def _strip_source_prefix(source: str) -> str:
    """Strip ``org:`` / ``url:`` / ``file:`` prefix from a source label."""
    for prefix in ("org:", "url:", "file:"):
        if source.startswith(prefix):
            return source[len(prefix) :]
    return source


def _format_age(seconds: int | None) -> str:
    """Render a cache age in a compact, human-friendly way."""
    if seconds is None or seconds < 0:
        return "n/a"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _count_rules(policy: ApmPolicy | None) -> dict[str, int]:
    """Count actionable rules across every top-level policy section.

    Returns a flat dict keyed by ``<section>_<axis>`` with integer values.
    Allow-lists report ``-1`` to distinguish "no opinion" (``None``) from
    "explicitly empty" (``0``); callers are expected to render the
    difference.
    """
    if policy is None:
        return {}

    def _allow_count(value: tuple | None) -> int:
        return -1 if value is None else len(value)

    return {
        "dependencies_deny": len(policy.dependencies.deny),
        "dependencies_allow": _allow_count(policy.dependencies.allow),
        "dependencies_require": len(policy.dependencies.require),
        "mcp_deny": len(policy.mcp.deny),
        "mcp_allow": _allow_count(policy.mcp.allow),
        "mcp_transports_allowed": _allow_count(policy.mcp.transport.allow),
        "compilation_targets_allowed": _allow_count(policy.compilation.target.allow),
        "manifest_required_fields": len(policy.manifest.required_fields),
        "unmanaged_files_directories": len(policy.unmanaged_files.directories),
    }


def _summarize_rules(counts: dict[str, int]) -> list[str]:
    """Render rule counts as a list of one-line human summaries.

    Skips axes that report ``-1`` (no opinion) or ``0``.  An axis with
    explicit empty allow-list (``0`` returned through a separate channel)
    is intentionally omitted from the human summary -- the JSON view keeps
    full fidelity.
    """
    labels = [
        ("dependencies_deny", "dependency denies"),
        ("dependencies_allow", "dependency allow patterns"),
        ("dependencies_require", "required dependencies"),
        ("mcp_deny", "mcp denies"),
        ("mcp_allow", "mcp allow patterns"),
        ("mcp_transports_allowed", "mcp transport restrictions"),
        ("compilation_targets_allowed", "compilation target restrictions"),
        ("manifest_required_fields", "required manifest fields"),
        ("unmanaged_files_directories", "unmanaged-file directories"),
    ]
    summary: list[str] = []
    for key, label in labels:
        value = counts.get(key, -1)
        if value > 0:
            summary.append(f"{value} {label}")
    return summary


def _resolve_chain_refs(result: PolicyFetchResult, project_root: Path) -> list[str]:
    """Best-effort lookup of the resolved ``extends`` chain for ``result``.

    For org / URL fetches the chain is persisted in cache meta.json by
    ``discover_policy_with_chain``; we read it back via the cache key.
    For file overrides (no cache) we fall back to the leaf's declared
    ``policy.extends`` value when present.
    """
    if result.policy is None:
        return []

    # Try cache lookup first (org / URL paths populate chain_refs in meta).
    repo_ref = _strip_source_prefix(result.source)
    if repo_ref and not result.source.startswith("file:"):
        try:
            entry = _read_cache_entry(repo_ref, project_root, ttl=MAX_STALE_TTL)
            if entry is not None and entry.chain_refs:
                # Drop the leaf itself from the visible chain.
                tail = [r for r in entry.chain_refs if r != repo_ref]
                if tail:
                    return tail
        except Exception:
            pass

    # Fallback: declared extends on the merged/leaf policy.
    if result.policy.extends:
        return [result.policy.extends]
    return []


def _enforcement_label(result: PolicyFetchResult) -> str:
    """Map a fetch result to a stable enforcement label for the report."""
    if result.outcome == "disabled":
        return "off"
    if result.policy is None:
        return "n/a"
    enf = result.policy.enforcement or "warn"
    if enf not in ("block", "warn", "off"):
        return enf
    return enf


def _cache_age_label(result: PolicyFetchResult) -> str:
    """Render the cache-age column with stale / refresh-failure context."""
    if result.outcome == "disabled" or (result.policy is None and not result.cached):
        return "n/a"
    age = _format_age(result.cache_age_seconds)
    if result.cache_stale and result.fetch_error:
        return f"stale ({age}, refresh failed: {result.fetch_error})"
    if result.cache_stale:
        return f"stale ({age})"
    if result.cached:
        return age
    return "fresh fetch"


def _build_report(
    result: PolicyFetchResult,
    chain: list[str],
    counts: dict[str, int],
) -> dict[str, Any]:
    """Assemble the structured report consumed by both renderers."""
    source_label = result.source if result.source else "n/a"
    if result.outcome in ("absent", "no_git_remote", "disabled"):
        source_label = source_label or "n/a"

    return {
        "outcome": result.outcome or "unknown",
        "source": source_label,
        "enforcement": _enforcement_label(result),
        "cache_age_seconds": result.cache_age_seconds,
        "cache_age_human": _cache_age_label(result),
        "cache_stale": bool(result.cache_stale),
        "cached": bool(result.cached),
        "fetch_error": result.fetch_error,
        "error": result.error,
        "extends_chain": chain,
        "rule_counts": counts,
        "rule_summary": _summarize_rules(counts),
    }


# -- Renderers ------------------------------------------------------


def _render_json(report: dict[str, Any]) -> None:
    """Emit the report as a single JSON object on stdout."""
    click.echo(json.dumps(report, indent=2, sort_keys=True))


def _render_table(report: dict[str, Any]) -> None:
    """Render the report as a Rich table (with a plain-text fallback)."""
    rows = [
        ("Outcome", report["outcome"]),
        ("Source", report["source"]),
        ("Enforcement", report["enforcement"]),
        ("Cache age", report["cache_age_human"]),
        (
            "Extends chain",
            ", ".join(report["extends_chain"]) if report["extends_chain"] else "none",
        ),
        (
            "Effective rules",
            "; ".join(report["rule_summary"]) if report["rule_summary"] else "none",
        ),
    ]

    console = _get_console()
    if RICH_AVAILABLE and Table is not None and console is not None:
        try:
            table = Table(
                title="APM Policy Status",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Field", style="bold white", no_wrap=True)
            table.add_column("Value", style="white")
            for field_name, value in rows:
                table.add_row(field_name, value)
            console.print(table)
            if report.get("error"):
                _rich_panel(
                    f"Discovery error: {report['error']}",
                    title="Notice",
                    style="yellow",
                )
            return
        except Exception:
            pass

    # Plain-text fallback (still ASCII-only).
    click.echo("APM Policy Status")
    click.echo("-----------------")
    for field_name, value in rows:
        click.echo(f"  {field_name:15s}  {value}")
    if report.get("error"):
        click.echo(f"\nNotice: Discovery error: {report['error']}")


# -- CLI surface ----------------------------------------------------


@click.group(help="Inspect and diagnose APM policy")
def policy():
    """APM policy diagnostics and (future) tooling."""
    pass


@policy.command(
    "status",
    help="Show the current policy posture (discovery, cache, rules)",
)
@click.option(
    "--policy-source",
    "policy_source",
    default=None,
    help=f"Override discovery. {POLICY_SOURCE_FORMS_HELP}",
)
@click.option(
    "--no-cache",
    "no_cache",
    is_flag=True,
    help="Force a fresh fetch (skip the policy cache).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the report as JSON (alias of -o json).",
)
@click.option(
    "-o",
    "--output",
    "output_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    help="Output format (default: table).",
)
@click.option(
    "--check",
    "check",
    is_flag=True,
    help=(
        "Exit non-zero (1) when no usable policy is found. "
        "Use in CI to gate on policy resolvability; default exit is "
        "always 0 for human / SIEM use."
    ),
)
def status(policy_source, no_cache, as_json, output_format, check):
    """Render a diagnostic snapshot of the active APM policy.

    Default exit code is always 0 -- discovery failures are reported in
    the output, never via process exit code, so the command is safe for
    human and SIEM use. Pass ``--check`` to make the command exit 1 when
    no usable policy is resolved (anything other than ``outcome=found``),
    which is suitable for CI pre-checks.
    """
    logger = CommandLogger("policy status")
    project_root = Path.cwd()

    try:
        if policy_source is not None:
            result = discover_policy(
                project_root,
                policy_override=policy_source,
                no_cache=no_cache,
            )
        elif no_cache:
            # discover_policy_with_chain has no `no_cache` knob, so go
            # through the lower-level entry point when the user opts out.
            result = discover_policy(project_root, no_cache=True)
        else:
            result = discover_policy_with_chain(project_root)
    except Exception as e:
        # Diagnostic must never exit non-zero; surface the failure as a
        # synthetic ``cache_miss_fetch_fail`` report and continue.
        _rich_error(
            f"Unexpected error while resolving policy: {e}",
            symbol="error",
        )
        result = PolicyFetchResult(
            outcome="cache_miss_fetch_fail",
            error=str(e),
            fetch_error=str(e),
        )

    chain = _resolve_chain_refs(result, project_root)
    counts = _count_rules(result.policy)
    report = _build_report(result, chain, counts)

    use_json = as_json or output_format.lower() == "json"
    try:
        if use_json:
            _render_json(report)
        else:
            _render_table(report)
    except Exception as e:
        _rich_error(f"Failed to render policy status: {e}", symbol="error")
        # Even render failure must not change exit code.
    finally:
        logger.render_summary()

    if check and report["outcome"] != "found":
        sys.exit(1)
    sys.exit(0)
