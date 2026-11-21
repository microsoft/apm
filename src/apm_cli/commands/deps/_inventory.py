"""Read-only per-package inventory for ``apm deps list``.

Reports two facts per installed package without mutating anything:

* **Targets** -- the deployment targets the lockfile's deployment ledger
  records for the package in the listed scope.
* **MCPs** -- the number of MCP servers the package declares.

Targets come from recorded ledger state only. They are never inferred from the
configured target list or from directory names. Package contents (MCPs) and
recorded deployment (targets) are reported separately and say nothing about
live runtime health.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ...core.scope import manifest_file
from ...utils.yaml_io import load_yaml

if TYPE_CHECKING:
    from ...agent_plugins.ir import AgentPlugin
    from ...core.deployment_state import DeploymentRecord

# Ledger service rows for MCP servers use this locator target (the runtime is
# the client the server was written to).
_MCP_SERVICE = "mcp"

# Why a Targets cell is marked ``*``. These are the ``TargetsResult.notes`` values.
NOTE_LEGACY = "legacy"
NOTE_MCP_SCOPE = "unattributed MCP"
NOTE_MCP_UNREADABLE = "unreadable MCP"

# Legend wording per cause, in the order causes are listed.
_CAUSE_TEXT = {
    NOTE_LEGACY: "legacy records",
    NOTE_MCP_SCOPE: "MCP servers recorded for the whole scope",
    NOTE_MCP_UNREADABLE: "MCP definitions couldn't be read",
}

# Inspection failures: the package or its manifest cannot be read or is invalid.
# ``ValueError`` covers ``UnicodeDecodeError``, ``AgentPluginError`` and
# ``PathTraversalError``; ``OSError`` covers permission and missing-path errors.
# Anything else is a programming error and must propagate.
_INSPECTION_FAILURES = (OSError, ValueError, yaml.YAMLError)

# Plugin MCP diagnostics that mean the whole MCP document was disabled (as
# opposed to one invalid server being dropped): the count cannot be trusted.
_MCP_DOCUMENT_UNREADABLE_CODES = frozenset(
    {"mcp.location.ambiguous", "mcp.location.invalid", "mcp.document.invalid"}
)


class TargetEvidence(str, Enum):
    """How much the recorded deployment state says about a package's targets."""

    KNOWN = "known"
    """Every recorded row for the package names a concrete target."""

    UNAVAILABLE = "unavailable"
    """No attributable record: no canonical ledger, or content with no row."""

    AMBIGUOUS = "ambiguous"
    """Rows exist but none names a concrete target (all unattributed)."""

    PARTIAL = "partial"
    """Some rows name a concrete target; others are unattributed."""


@dataclass(frozen=True)
class TargetsResult:
    """Recorded deployment targets for one package in one scope."""

    evidence: TargetEvidence
    targets: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    """Why a ``PARTIAL`` result is partial (the ``NOTE_*`` constants)."""


@dataclass(frozen=True)
class McpResult:
    """MCP servers declared by a package manifest.

    ``count`` is ``None`` when the package could not be inspected, which is
    different from a successfully inspected package that declares ``0``.
    """

    count: int | None


def package_targets(
    records: tuple[DeploymentRecord, ...] | None,
    *,
    mcp_count: int | None = 0,
    has_primitives: bool = False,
) -> TargetsResult:
    """Return the recorded deployment targets a package's own rows support.

    A pure function of the package's already-resolved ledger rows.

    Args:
        records: The rows the package holds in the listed scope, from
            ``DeploymentLedgerCodec.recorded_owner_index``; ``()`` when the
            ledger is recorded and the package holds none. ``None`` when there
            is nothing to read: no recorded deployment evidence (a ledger
            synthesized from legacy ``deployed_files`` is none), no lockfile, or
            a package that is not in the lockfile.
        mcp_count: MCP servers the package declares (``None`` if they could
            not be read, which keeps MCP deployments from being ruled out).
        has_primitives: Whether the package ships any non-MCP primitive.

    A record counts for every package in its ``owners``; the index applies that
    and the canonical scope test before this function sees the rows.

    Shared roots: several targets can deploy into one directory (for example
    ``.agents/skills`` for Codex, agent-skills and Antigravity; see
    ``integration/targets.py``). A row is credited only to the target recorded
    on that row. The directory is never used to infer a target: a shared-root
    row recorded under ``legacy`` stays unattributed.

    MCP deployments are recorded per scope and runtime under the root owner,
    so they identify a package's targets only when a row names the package as
    an owner. A package that declares MCP servers without such a row has an
    unattributed MCP part: ``unknown`` when nothing else is attributable,
    otherwise partial. An unreadable MCP count is treated the same way: it is
    reported as its own cause. ``-`` (known, empty) is returned only when the
    package has nothing to deploy and nothing recorded.

    Returns:
        The evidence level, the concrete targets credited to the package and,
        for a partial result, the causes (``NOTE_*``) behind the ``*`` marker.
    """
    from ...core.deployment_ledger import UNATTRIBUTED_TARGET
    from ...integration.targets import KNOWN_TARGETS

    if records is None:
        return TargetsResult(TargetEvidence.UNAVAILABLE)

    named: set[str] = set()
    unattributed = False
    mcp_attributed = False
    for record in records:
        locator = record.locator
        if locator.target == UNATTRIBUTED_TARGET:
            unattributed = True
        elif locator.target in KNOWN_TARGETS:
            named.add(locator.target)
        elif locator.target == _MCP_SERVICE and locator.runtime in KNOWN_TARGETS:
            named.add(locator.runtime)
            mcp_attributed = True
    targets = tuple(sorted(named))
    mcp_unattributed = bool(mcp_count) and not mcp_attributed
    # With the MCP count unreadable, MCP deployments cannot be ruled out.
    mcp_unreadable = mcp_count is None and not mcp_attributed
    notes = tuple(
        note
        for note, applies in (
            (NOTE_LEGACY, unattributed),
            (NOTE_MCP_SCOPE, mcp_unattributed),
            (NOTE_MCP_UNREADABLE, mcp_unreadable),
        )
        if applies
    )
    if unattributed and not targets:
        return TargetsResult(TargetEvidence.AMBIGUOUS)
    if notes and targets:
        return TargetsResult(TargetEvidence.PARTIAL, targets, notes)
    if not targets and (mcp_unattributed or mcp_unreadable or has_primitives):
        # Something is (or may be) deployable but nothing is attributable.
        return TargetsResult(TargetEvidence.UNAVAILABLE)
    return TargetsResult(TargetEvidence.KNOWN, targets)


def _manifest_mcp_count(manifest_path: Path) -> McpResult:
    """Count the servers declared by an installed ``apm.yml``."""
    from ...integration.mcp_config_view import authored_mcp_declarations

    manifest = load_yaml(manifest_path)
    if manifest is not None and not isinstance(manifest, dict):
        return McpResult(None)
    try:
        return McpResult(len(authored_mcp_declarations(manifest or {})))
    except (TypeError, AttributeError) as exc:
        # The canonical MCP model rejects most bad input with ``ValueError`` but
        # lets a wrongly typed field (``name: 5``, ``headers: 3``) surface as
        # ``TypeError`` / ``AttributeError``. That is malformed content, so it is
        # an inspection failure, and only the parser call is guarded this way.
        raise ValueError(f"invalid MCP declaration in {manifest_path}") from exc


def _plugin_mcp_count(plugin: AgentPlugin) -> McpResult:
    """Count the servers of a native Agent Plugin through its canonical loader."""
    if any(item.code in _MCP_DOCUMENT_UNREADABLE_CODES for item in plugin.diagnostics):
        return McpResult(None)
    return McpResult(len(plugin.components.mcp_servers))


def _unmanifested_mcp_count(package_dir: Path) -> McpResult:
    """Count servers for a package that is neither a plugin nor has an ``apm.yml``."""
    from ...models.validation import PackageType, detect_package_type

    package_type, _ = detect_package_type(package_dir)
    if package_type in {
        PackageType.SKILL_BUNDLE,
        PackageType.CLAUDE_SKILL,
        PackageType.HOOK_PACKAGE,
    }:
        return McpResult(0)
    return McpResult(None)


def package_mcp_count(package_dir: Path) -> McpResult:
    """Count MCP servers declared by the installed package at *package_dir*.

    Follows the same package interpretation install uses, read-only:

    * A native Agent Plugin -- the servers its canonical loader models, via
      ``route_agent_plugin_package``, which applies install's format
      precedence: an apm.yml that is only metadata does not outrank the
      plugin, while one that declares dependencies does. A plugin whose whole
      MCP document was disabled counts as unreadable; one invalid server is
      simply not admitted.
    * ``apm.yml`` -- parsed through the canonical authored-MCP declarations.
      Claude and marketplace plugins are covered here: install folds their
      ``mcpServers`` / ``.mcp.json`` into the manifest it writes under
      ``apm_modules`` (``deps/plugin_parser.py``).
    * A package type that may legitimately have no ``apm.yml`` (skill bundle,
      Claude skill, hook package) declares none: ``0``.

    Never fetches a registry, connects to a server, or executes package
    content.

    Args:
        package_dir: The installed package directory.

    Returns:
        The server count, or ``McpResult(None)`` when the package cannot be
        inspected (unreadable or malformed manifest, invalid plugin metadata),
        so "inspected, none declared" (``0``) stays distinct. Only inspection
        failures are caught; other exceptions are programming errors and
        propagate.
    """
    from ...bundle.local_bundle import route_agent_plugin_package

    manifest_path = manifest_file(package_dir)
    try:
        detection = route_agent_plugin_package(package_dir)
        if detection is not None and detection.plugin is not None:
            return _plugin_mcp_count(detection.plugin)
        if manifest_path.is_file():
            return _manifest_mcp_count(manifest_path)
        return _unmanifested_mcp_count(package_dir)
    except _INSPECTION_FAILURES:
        return McpResult(None)


def format_targets(result: TargetsResult | None) -> str:
    """Render a targets cell, kept short.

    ``claude, codex`` (every record attributed), ``claude, codex*`` (some
    records are not), ``ambiguous``, ``unknown``, or ``-`` (nothing to deploy,
    nothing recorded). ``None`` (a record built without inventory data)
    renders as ``unknown``.
    """
    if result is None or result.evidence is TargetEvidence.UNAVAILABLE:
        return "unknown"
    if result.evidence is TargetEvidence.AMBIGUOUS:
        return "ambiguous"
    names = ", ".join(result.targets)
    if result.evidence is TargetEvidence.PARTIAL:
        return f"{names}*"
    return names or "-"


def format_mcps(result: McpResult | None) -> str:
    """Render an MCPs cell: a count (including ``0``) or an unavailable marker."""
    return "?" if result is None or result.count is None else str(result.count)


def targets_legend(results: Iterable[TargetsResult | None]) -> str | None:
    """Return a one-line key for the Targets cells in this listing, or ``None``.

    Only what is present is explained: the causes behind ``*`` cells, and
    ``unknown`` / ``ambiguous`` when such a cell exists. Returns ``None`` when
    every cell is fully attributed (or ``-``).
    """
    causes: set[str] = set()
    unattributable = False
    for result in results:
        if result is None or result.evidence in (
            TargetEvidence.UNAVAILABLE,
            TargetEvidence.AMBIGUOUS,
        ):
            unattributable = True
        elif result.evidence is TargetEvidence.PARTIAL:
            causes.update(result.notes)
    parts = []
    if causes:
        listed = ", ".join(text for note, text in _CAUSE_TEXT.items() if note in causes)
        parts.append(f"* some deployments can't be attributed to a target: {listed}")
    if unattributable:
        parts.append("unknown/ambiguous: none can be attributed")
    return f"Targets: {'; '.join(parts)}." if parts else None
