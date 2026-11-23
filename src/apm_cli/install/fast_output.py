"""What a fast install prints: the full pipeline's lines, through the same owners.

The install fast paths (the no-op and diff-apply engine) skip the pipeline but
must print exactly what it prints for the same outcome (S3 output parity).
Every line here comes from the owner the pipeline uses for it:

- the header (``InstallLogger.resolution_start``) and the target provenance
  line (``core.target_detection``);
- one cached-package header per locked package in install order (depth, then
  identity: the resolver's ``_select_dependency_winners`` order), followed by
  the engine's per-package body, or ``(files unchanged)`` when nothing of it
  was written;
- the pre-deploy merged MCP policy check (``commit_gate``) and the MCP/LSP
  service reconciliation itself (``run_service_integrations``), which print
  the MCP block and parked-server warnings and repair a drifted config the way
  the full run does;
- the post-install summary (``install.summary``).
"""

from __future__ import annotations

import contextlib
import io
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

PackageBody = Callable[[Any], None]


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Silence a fast path's own verification: its readers' notices are printed
    by the phases ``render`` runs, in the full run's order (a policy miss, for
    one, is announced by the policy gate, not by the first trust reader)."""
    import io
    import logging

    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous)


class _Held(io.TextIOBase):
    """A stand-in stream that tapes writes, in order, for one real stream."""

    def __init__(self, real: Any, tape: list[tuple[Any, str]]) -> None:
        self._real, self._tape = real, tape

    def write(self, text: str) -> int:
        self._tape.append((self._real, text))
        return len(text)

    def isatty(self) -> bool:
        return bool(getattr(self._real, "isatty", lambda: False)())

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._real, "encoding", None) or "utf-8"


class HeldOutput:
    """What a fast apply printed, kept until the apply is final."""

    def __init__(self) -> None:
        self.tape: list[tuple[Any, str]] = []

    def release(self) -> None:
        """Print the held output to the real streams, in the order it was written."""
        tape, self.tape = self.tape, []
        for stream, text in tape:
            stream.write(text)
        for stream in {id(s): s for s, _t in tape}.values():
            stream.flush()


@contextlib.contextmanager
def held_output() -> Iterator[HeldOutput]:
    """Hold stdout and stderr: a fallback to the full run drops them unprinted."""
    import sys

    held = HeldOutput()
    out, err = _Held(sys.stdout, held.tape), _Held(sys.stderr, held.tape)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield held


def render(
    project_root: Path,
    lock: Any,
    manifest: Any,
    *,
    started: float,
    bodies: Mapping[str, PackageBody] | None = None,
    installed: int = 0,
    frozen_disposition: Any,
    fetched: frozenset[str] = frozenset(),
) -> Any:
    """Print the install output for an already applied (or empty) diff.

    *bodies* maps a dependency key to the callable that prints that package's
    integration lines (given the ``InstallLogger``); a package without one
    printed ``(files unchanged)``. *installed* is the APM dependency count the
    summary reports. Returns the service reconciliation result.
    """
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.install.summary import render_post_install_summary

    logger = InstallLogger(verbose=False, dry_run=False, partial=False)
    logger.resolution_start(to_install_count=0, lockfile_count=0, update_count=0)
    locked = [d for d in lock.get_all_dependencies() if d.get_unique_key() != "."]
    for dep in locked:
        if dep.get_unique_key() in fetched:
            # The resolve phase's heartbeat for a package it materializes.
            logger.resolving_heartbeat(dep.to_dependency_ref().get_display_name())
    policy_fetch = run_policy_gate(project_root, manifest, lock, logger)
    _announce_targets(project_root, manifest, logger)
    for dep in locked:
        key = dep.get_unique_key()
        _package_header(dep, logger, cached=key not in fetched)
        body = (bodies or {}).get(key)
        if body is None:
            logger.tree_item("  |-- (files unchanged)")
        else:
            body(logger)
    from apm_cli.install.helpers.gitignore import update_gitignore_for_apm_modules

    # The full run's point (install/pipeline.py, after integrate, project scope):
    # it adds apm_modules/ and says so only when the entry is missing. With no
    # APM package the full run returns before integrate and leaves it alone.
    if locked:
        update_gitignore_for_apm_modules(logger=logger)
    services = run_services(
        project_root, manifest, lock, logger, policy_fetch, frozen_disposition=frozen_disposition
    )
    result = render_post_install_summary(
        logger=logger,
        apm_count=installed,
        mcp_count=services.mcp_count,
        lsp_count=services.lsp_count,
        apm_diagnostics=logger.diagnostics,
        force=False,
        elapsed_seconds=time.perf_counter() - started,
    )
    if frozen_disposition.replay and lock.dependencies:
        from apm_cli.install.outcome import frozen_verified_line

        # --frozen verified the lockfile and deployed hashes (req-lk-017).
        logger.info(frozen_verified_line(result), symbol="info")
    return services


def _announce_targets(project_root: Path, manifest: Any, logger: Any) -> None:
    from apm_cli.core.target_detection import format_provenance, resolve_targets
    from apm_cli.models.apm_package import package_target_declaration

    resolved = resolve_targets(
        project_root, flag=None, yaml_targets=package_target_declaration(manifest).restriction()
    )
    logger.announce_targets(format_provenance(resolved))


def _package_header(dep: Any, logger: Any, *, cached: bool = True) -> None:
    """The cached-source header ``install/sources.py`` prints for a locked package."""
    from apm_cli.utils.short_sha import format_short_sha

    dep_ref = dep.to_dependency_ref()
    name = str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
    ref = dep.resolved_ref or dep_ref.reference or ""
    sha = format_short_sha(dep.resolved_commit) if dep.resolved_commit else ""
    logger.download_complete(name, ref=ref, sha=sha, cached=cached)


def run_policy_gate(project_root: Path, manifest: Any, lock: Any, logger: Any) -> Any:
    """The install's policy-gate phase itself (``install/phases/policy_gate.py``).

    It announces the org policy outcome exactly as the full run does (and
    records the banner, so later trust readers do not repeat it), then runs
    the dependency policy checks against the locked set. Returns the fetch.
    """
    from types import SimpleNamespace

    from apm_cli.core.scope import InstallScope
    from apm_cli.install.phases import policy_gate

    locked = [dep.to_dependency_ref() for key, dep in lock.dependencies.items() if key != "."]
    gate = SimpleNamespace(
        project_root=project_root,
        scope=InstallScope.PROJECT,
        apm_package=manifest,
        logger=logger,
        no_policy=False,
        dry_run=False,
        only_packages=None,
        deps_to_install=locked,
        all_apm_deps=list(manifest.get_apm_dependencies()),
        existing_lockfile=lock,
        dependency_graph=None,
        declared_direct_refs=None,
        marketplace_provenance=None,
        prefetch_policy_denials=[],
        direct_mcp_deps=list(manifest.get_all_mcp_dependencies()),
        policy_fetch=None,
        policy_enforcement_active=False,
    )
    policy_gate.run(gate)
    return gate.policy_fetch


def run_services(
    project_root: Path,
    manifest: Any,
    lock: Any,
    logger: Any,
    policy_fetch: Any = None,
    *,
    frozen_disposition: Any,
) -> Any:
    """The install's MCP/LSP reconciliation phase itself, as a plain install runs it."""
    from types import SimpleNamespace

    from apm_cli.constants import InstallMode
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.service_integration import run_service_integrations
    from apm_cli.install.template import effective_allow_map
    from apm_cli.policy.outcome_routing import discover_trust_policy

    trust = SimpleNamespace(
        policy_fetch=policy_fetch
        if policy_fetch is not None
        else discover_trust_policy(project_root),
        source_root=project_root,
        project_root=project_root,
        scope=InstallScope.PROJECT,
        apm_package=manifest,
        logger=logger,
        dry_run=False,
        exec_trust_ctx=None,
        exec_allow_map=None,
    )
    allow_map = effective_allow_map(trust)
    check_merged_mcp(project_root, manifest, lock, allow_map, logger)
    command = SimpleNamespace(
        project_root=project_root,
        scope=InstallScope.PROJECT,
        install_mode=InstallMode.ALL,
        logger=logger,
        runtime=None,
        exclude=None,
        trust_transitive_mcp=False,
        no_policy=False,
        verbose=False,
        force=False,
        update=False,
        exec_allow_map=allow_map,
        exec_allow_resolved=trust.exec_trust_ctx is not None,
        mcp_policy_enforced=True,
    )
    mcp_deps = manifest.get_all_mcp_dependencies()
    return run_service_integrations(
        command,
        apm_package=manifest,
        mcp_deps=mcp_deps if isinstance(mcp_deps, list) else [],
        lock_path=get_lockfile_path(project_root),
        existing_lock=lock,
        old_mcp_servers=set(lock.mcp_servers),
        old_mcp_configs=dict(lock.mcp_configs),
        old_mcp_provenance=dict(lock.mcp_config_provenance),
        old_mcp_target_servers=dict(lock.mcp_target_servers),
        old_mcp_target_servers_present=lock._mcp_target_servers_present,
        diagnostics=logger.diagnostics,
        explicit_target=None,
        target_decision=None,
        active_package_keys=frozenset(k for k in lock.dependencies if k != "."),
        frozen_disposition=frozen_disposition,
    )


def check_merged_mcp(
    project_root: Path, manifest: Any, lock: Any, allow_map: Any, logger: Any
) -> None:
    """The pre-deploy merged MCP policy check, as ``check_before_deploy`` runs it.

    Same owner calls and the same silent collection logger
    (``commit_gate._prepare_merged_mcp``/``_check_merged_mcp_policy``); the
    prospective lockfile of a fast install is the locked one. It also consumes
    the once-per-command trust notice exactly as the full run does.
    """
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.commit_gate import (
        _AnnouncedPolicyLogger,
        _SilentLogger,
        enforce_mcp_policy,
    )
    from apm_cli.install.mcp.integration import prepare_mcp_integration

    prepared = prepare_mcp_integration(
        apm_package=manifest,
        mcp_deps=list(manifest.get_all_mcp_dependencies()),
        apm_modules_path=project_root / APM_MODULES_DIR,
        lock_path=get_lockfile_path(project_root),
        should_install=True,
        trust_transitive_mcp=False,
        logger=_SilentLogger(),
        active_package_keys=frozenset(k for k in lock.dependencies if k != "."),
        user_scope=False,
        project_root=project_root,
        executable_context=(allow_map, True),
        no_policy=False,
        claude_native_keys=frozenset(),
        prospective_lock=lock,
    )
    view = prepared.current_view
    enforce_mcp_policy(
        list(prepared.dependencies),
        brought_in_by=view.brought_in_by if view is not None else None,
        runtime=None,
        target_decision=None,
        user_scope=False,
        project_root=project_root,
        no_policy=False,
        logger=_AnnouncedPolicyLogger(logger),
        dry_run=False,
    )
