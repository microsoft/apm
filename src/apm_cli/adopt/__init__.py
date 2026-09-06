"""Brownfield adoption: discover existing agent-harness files and import them.

Entry point for ``apm init --discover`` and the hidden ``apm discover`` alias.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.core.command_logger import CommandLogger
from apm_cli.core.scope import USER_APM_DIR
from apm_cli.core.target_catalog import manifest_target_names
from apm_cli.core.target_detection import CANONICAL_TARGETS_ORDERED, detect_signals
from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile

from .classify import classify
from .model import AdoptionReport, Finding, HarnessKind, Importability, Ownership, Scope
from .ownership import OwnershipIndex
from .redact import Redactor
from .registry import REGISTRY, ScanContext, ScanLimits
from .render import render
from .scanners import register_builtin_scanners

_ORDER = {name: index for index, name in enumerate(CANONICAL_TARGETS_ORDERED)}


def scan_targets(scope: Scope) -> tuple[TargetProfile, ...]:
    """Return the stable, statically-rooted profiles to read back for *scope*."""
    profiles: list[TargetProfile] = []
    for profile in KNOWN_TARGETS.values():
        if (
            profile.capability.experimental_flag is not None
            or profile.user_root_resolver is not None
        ):
            continue
        scoped = profile.for_scope(user_scope=scope is Scope.USER)
        if scoped is not None:
            profiles.append(scoped)
    return tuple(profiles)


def _sort_key(finding: Finding) -> tuple:
    return (
        _ORDER.get(finding.tool, len(_ORDER)),
        finding.tool,
        finding.kind.value,
        finding.display_path,
    )


def discover(root: Path, scope: Scope, *, limits: ScanLimits | None = None) -> AdoptionReport:
    """Run every registered scanner for one scope and classify the results."""
    register_builtin_scanners()
    redactor = Redactor(
        root if scope is Scope.PROJECT else Path.cwd(), home=root if scope is Scope.USER else None
    )
    ctx = ScanContext(
        root=root,
        scope=scope,
        targets=scan_targets(scope),
        redactor=redactor,
        limits=limits or ScanLimits(),
    )
    ownership = OwnershipIndex.build(root, scope)
    findings: list[Finding] = []
    for raw in REGISTRY.scan_all(ctx):
        owner, evidence = ownership.decide(raw)
        findings.append(classify(raw, owner, evidence))
    findings.sort(key=_sort_key)

    detected = (
        {signal.target for signal in detect_signals(root)} if scope is Scope.PROJECT else set()
    )
    detected |= {
        f.tool
        for f in findings
        if f.tool in KNOWN_TARGETS and f.ownership is not Ownership.APM_GENERATED
    }
    manifest_safe = manifest_target_names()
    proposed_targets = [
        t for t in sorted(detected, key=lambda n: (_ORDER.get(n, 99), n)) if t in manifest_safe
    ]
    if scope is Scope.USER:
        proposed_targets = [t for t in proposed_targets if KNOWN_TARGETS[t].user_supported]
    proposed_mcp = tuple(
        {"name": f.payload["name"], "source": f.payload.get("config_path", f.tool)}
        for f in findings
        if f.kind is HarnessKind.MCP_SERVER
        and f.importability is Importability.CONVERTIBLE
        and isinstance(f.payload, dict)
    )
    manifest = (
        root / APM_YML_FILENAME
        if scope is Scope.PROJECT
        else root / USER_APM_DIR / APM_YML_FILENAME
    )
    return AdoptionReport(
        project_root_display=redactor.path(root, scope) or ".",
        scopes=(scope,),
        detected_tools=tuple(sorted(detected, key=lambda n: (_ORDER.get(n, 99), n))),
        findings=tuple(findings),
        errors=tuple(ctx.errors),
        proposed_targets=tuple(proposed_targets),
        proposed_mcp=proposed_mcp,
        apm_yml_exists=manifest.is_file(),
    )


def run_discover_command(
    *,
    project_root: Path,
    write: bool,
    fmt: str,
    user_scope: bool,
    target_flag: object | None,
    yes: bool,
    verbose: bool,
    include_hook_scripts: bool,
) -> int:
    """CLI body shared by ``apm init --discover`` and ``apm discover``."""
    logger = CommandLogger("init", verbose=verbose)
    scope = Scope.USER if user_scope else Scope.PROJECT
    root = Path.home() if user_scope else project_root
    if fmt == "text":
        verb = "Importing" if write else "Discovering"
        logger.start(f"{verb} agent context ({scope.value} scope)", symbol="running")
    report = discover(root, scope)
    next_steps: list[str] = []
    if not write:
        if any(
            f.importability in (Importability.APM_NATIVE, Importability.CONVERTIBLE)
            for f in report.findings
        ):
            next_steps.append(
                "Preview only. Re-run with --apply to import into .apm/ and update apm.yml"
            )
        render(report, fmt, logger, next_steps)
        return 0
    from .materialize import run_write

    return run_write(
        report,
        root=root,
        scope=scope,
        fmt=fmt,
        logger=logger,
        yes=yes,
        target_flag=target_flag,
        include_hook_scripts=include_hook_scripts,
    )


__all__ = ["AdoptionReport", "discover", "run_discover_command", "scan_targets"]
