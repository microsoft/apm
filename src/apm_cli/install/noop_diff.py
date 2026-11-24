"""Stateless no-op install: exit early when the desired state is already deployed.

``apm install`` on an unchanged project runs the whole pipeline and then
reports ``No changes``. This module decides the same answer from state alone,
with no memo: the desired state is the lockfile plus the package copies in
``apm_modules``; the actual state is the deployed files and configs. When the
two agree and every live gate still admits them, nothing would change.

Every check reads current bytes (content hashes, never mtimes) and every gate
is evaluated live on each run:

- the lockfile was written by this APM version (rendering is unchanged);
- org policy is discovered and routed live; any blocking, unresolved, stale,
  failed or warning outcome takes the full pipeline, which reports it;
- every locked package copy in ``apm_modules`` exists and proves its locked
  content (the install copy-validity owner);
- the ``apm audit --ci`` baseline owner passes: manifest/lock identity,
  deployment ledger owners, deployed files present, no orphans, targets
  current, skill subsets, MCP config consistency, native MCP ownership,
  deployed content hashes, local-bundle hooks, hook approval pins and the
  executables gate (approvals and denylist) against the live policy;
- when a policy is in force, its dependency and disk checks all pass.

Anything outside that set (local, registry or marketplace sources, LSP
servers, a policy warning, any failing or warning check) runs the full
pipeline. ``APM_INSTALL_FAST_PATH=0`` turns the early exit off.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

DISABLE_ENV = "APM_INSTALL_FAST_PATH"
_DISABLED = frozenset({"0", "false", "no", "off"})


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in _DISABLED


# Options a no-op may carry; any other set option takes the full pipeline.
_NEUTRAL_OPTIONS = frozenset({"frozen", "parallel_downloads"})


def eligible(options: dict[str, Any], argv: list[str] | None) -> bool:
    """Only a plain project re-install of the manifest (optionally ``--frozen``)."""
    if disabled() or argv is None:
        return False
    if any(a == "--policy" or a.startswith("--policy=") for a in argv):
        return False
    return not any(
        value not in (None, False, (), [], "")
        for name, value in options.items()
        if name not in _NEUTRAL_OPTIONS
    )


def _policy_admits(project_root: Path) -> tuple[bool, Any]:
    """Discover org policy live; (admits, fetch result)."""
    from apm_cli.policy.discovery import policy_disabled_by_env
    from apm_cli.policy.outcome_routing import classify_project_policy, discover_trust_policy

    if policy_disabled_by_env():
        return False, None
    fetch = discover_trust_policy(project_root)
    route = classify_project_policy(project_root, fetch_result=fetch)
    if route.blocks or route.unresolved:
        return False, fetch
    if fetch.cache_stale or fetch.fetch_error or fetch.warnings:
        return False, fetch
    return True, fetch


def _copies_valid(project_root: Path, lock: Any) -> bool:
    """Every locked remote package copy exists and proves its locked content."""
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.audit_replay import _live_copies_stale

    modules = project_root / APM_MODULES_DIR
    for key, dep in lock.dependencies.items():
        if key == ".":
            continue
        if getattr(dep, "source", None) is not None:
            return False  # local, registry, marketplace: the full pipeline decides
        try:
            if not dep.to_dependency_ref().get_install_path(modules).is_dir():
                return False
        except (ValueError, OSError):
            return False
    stale, _ = _live_copies_stale(modules, get_lockfile_path(project_root))
    return not stale


def _audit_passes(project_root: Path, fetch: Any) -> bool:
    from apm_cli.install.exec_gate import exec_policy_from_discovery
    from apm_cli.policy.ci_checks import run_baseline_checks

    result = run_baseline_checks(
        project_root,
        fail_fast=True,
        ci_mode=True,
        policy_provider=lambda: exec_policy_from_discovery(lambda: fetch),
    )
    return result.passed and not any(getattr(c, "warned", False) for c in result.checks)


def _policy_checks_pass(project_root: Path, fetch: Any) -> bool:
    policy = getattr(fetch, "policy", None) if fetch is not None and fetch.found else None
    if policy is None or getattr(policy, "enforcement", None) == "off":
        return True
    from apm_cli.policy.policy_checks import run_policy_checks

    result = run_policy_checks(project_root, policy, fail_fast=True)
    return all(check.passed for check in result.checks)


def up_to_date(project_root: Path) -> bool:
    """Whether a plain install of *project_root* would change nothing."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path, installed_apm_version

    lock = LockFile.read(get_lockfile_path(project_root))
    if lock is None or lock.apm_version != installed_apm_version():
        return False
    if lock.lsp_servers or lock.local_deployed_files:
        return False
    from apm_cli.native_plugins.claude_marketplaces import project_release_pending

    if project_release_pending(project_root):
        return False  # a Claude registry entry a failed uninstall left (#5258)
    admits, fetch = _policy_admits(project_root)
    return (
        admits
        and _copies_valid(project_root, lock)
        and _audit_passes(project_root, fetch)
        and _policy_checks_pass(project_root, fetch)
    )


def invocation_argv() -> list[str] | None:
    """The arguments the root group parsed (``cli.py``); never ``sys.argv``."""
    import click

    ctx = click.get_current_context(silent=True)
    raw = ctx.meta.get("apm_raw_args") if ctx is not None else None
    return list(raw) if isinstance(raw, (list, tuple)) else None


def try_noop(options: dict[str, Any], argv: list[str] | None, project_root: Path) -> bool:
    """Print the no-op summary and return True when nothing would change.

    Any error or doubt returns False: the caller runs the full pipeline.
    """
    started = time.perf_counter()
    if not eligible(options, argv):
        return False
    try:
        if not up_to_date(project_root):
            return False
    except Exception:
        return False
    from apm_cli.utils.console import _rich_info

    elapsed = time.perf_counter() - started
    _rich_info(f"No changes -- install state already up to date in {elapsed:.1f}s.", symbol="info")
    return True


def sources_scan_clean(project_root: Path, lock: Any) -> bool:
    """No package file the pre-deploy security scan reads carries any finding.

    The full pipeline re-scans each package's deployable sources on every
    install and prints its warnings even when nothing is redeployed. Scanning
    every file of each copy (a superset) with the same scanner keeps that
    verdict and output: any finding, of any severity, takes the full pipeline.
    A symlinked file or directory in a copy is a finding too: package hashes
    exclude symlinks and the source-plan owner skips them with a warning, so
    only the full pipeline may judge that state.
    """
    import os

    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.security.content_scanner import ContentScanner

    modules = project_root / APM_MODULES_DIR
    for key, dep in lock.dependencies.items():
        if key == ".":
            continue
        root = dep.to_dependency_ref().get_install_path(modules)
        if os.path.islink(root):
            return False
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if any(os.path.islink(os.path.join(directory, n)) for n in (*dirnames, *filenames)):
                return False
            for name in filenames:
                if ContentScanner.scan_file(Path(directory) / name):
                    return False
    return True
