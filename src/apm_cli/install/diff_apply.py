"""Diff-apply install (lane C, S2): materialize a locked project from the caches.

A ``--frozen`` or locked re-install of a project whose lockfile is current needs
no resolve and no rendering: the lockfile already names every package commit,
its content hash and every deployed file's hash. This module computes the diff
between that desired state and the disk, and fills it from the caches:

- package copies come from the read-only git cache (pinned commit, verified
  HEAD), are staged OUTSIDE the project and must prove their locked content
  there; the pre-deploy scan and the executables decision run on them too;
- deployed files come from the deploy store through package-bound render
  records, each re-hashed against the committed lockfile hash.

The decision is final before the first write: the org policy gate is discovered
live and the ``apm audit --ci`` baseline owner must pass (only the checks this
apply fills may fail). APPLY only creates files; it never deletes or replaces
anything. The writes, the receipts and the rendered output's own writes share one
journal: any failure restores the pre-run tree.
Anything uncertain (a store miss, an edited deployed file, a symlink, an escape
sequence, a lifecycle script, local or registry sources, any option) declines,
and the full pipeline runs untouched. ``APM_INSTALL_FAST_PATH=0`` turns it off;
``APM_INSTALL_FAST_PATH=trace`` prints one decision line.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Escapes a JSON/TOML/YAML reader decodes into codepoints the ASCII guard cannot see.
# The destination name the commit reserves a scratch slot for; never published.
_SCRATCH = ".apm-fast-apply"
_ESCAPE = re.compile(rb"\\(?:u|U|x|N\{)")


# Per-package integration body lines this process emitted (full run), by dependency key.
_BODIES: dict[str, list[str]] = {}


class _BodyLogger:
    """Delegates to the install logger and keeps one package's tree lines."""

    def __init__(self, inner: Any, body: list[str]) -> None:
        self._inner = inner
        self._body = body

    def tree_item(self, message: str, *args: Any, **kwargs: Any) -> Any:
        self._body.append(message)
        return self._inner.tree_item(message, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def capture_body(logger: Any, package_name: str) -> Any:
    """The logger one full-run package integration prints through, recording its body."""
    if logger is None:
        return None
    body: list[str] = []
    _BODIES[str(package_name)] = body
    return _BodyLogger(logger, body)


@dataclass(frozen=True)
class PackageOutcome:
    """One dependency of an applied diff, for the output renderer (install/fast_output.py)."""

    key: str
    dep: Any
    files_written: bool
    body: tuple[str, ...]


class Decline(Exception):
    """The fast path cannot prove the result: run the full pipeline."""


def _trace(message: str) -> None:
    if os.environ.get("APM_INSTALL_FAST_PATH", "").strip().lower() in {"trace", "decide"}:
        sys.stderr.write(f"[i] install fast path: {message}\n")


def _outcomes(lock: Any, files: list, records: dict[str, dict]) -> list[PackageOutcome]:
    """Per dependency: all of its files written (replay its recorded body), none, or decline."""
    written: dict[str, int] = {}
    for _path, _data, _digest, owner in files:
        written[owner] = written.get(owner, 0) + 1
    out: list[PackageOutcome] = []
    for key, dep in lock.dependencies.items():
        total = len(getattr(dep, "deployed_file_hashes", None) or {})
        count = written.get(key, 0)
        if count == 0:
            out.append(PackageOutcome(key, dep, False, ("  |-- (files unchanged)",)))
            continue
        if count != total:
            raise Decline("partial package diff")
        body = records.get(key, {}).get("body")
        if not isinstance(body, list) or not all(isinstance(line, str) for line in body):
            raise Decline("no recorded output")
        out.append(PackageOutcome(key, dep, True, tuple(body)))
    return out


# CI definitions live under the .github target root, but APM never deploys there.
_CI_DIRS = (".github/workflows/", ".github/actions/")


def _is_git_dir_name(part: str) -> bool:
    """``.git`` in any case, and its Windows aliases (trailing dots/spaces, 8.3 name)."""
    name = part.lower()
    return name.rstrip(". ") == ".git" or name == "git~1"


def _within(root: Path, rel: str, prefixes: tuple[str, ...] | None = None) -> Path:
    """The project path of a lock-named file, only inside an active target's deploy root.

    *prefixes* are the active targets' integration prefixes (the deploy-path
    owner's); the full pipeline writes nothing else, so neither does the fast
    run. A ``.git`` component is refused in any case.
    """
    parts = rel.split("/") if rel else []
    if not rel or rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts):
        raise Decline(f"unsafe path {rel!r}")
    windows_name = "/".join(p.lower().rstrip(". ") for p in parts) + "/"
    if any(_is_git_dir_name(p) for p in parts) or windows_name.startswith(_CI_DIRS):
        raise Decline(f"unsafe path {rel!r}")
    if prefixes is not None:
        from apm_cli.integration.base_integrator import BaseIntegrator

        if not BaseIntegrator.validate_deploy_path(rel, root, allowed_prefixes=prefixes):
            raise Decline(f"path outside the target roots {rel!r}")
    path = root / rel
    from apm_cli.utils.path_security import has_symlink_component

    # The owner also treats a Windows directory junction as a link (#5229).
    if has_symlink_component(root, path.parent):
        raise Decline("symlink in a deployed path")
    return path


def _package_records(lock: Any, targets: Any, deploy: Any) -> dict[str, dict]:
    """Each dependency's package-bound render record; it must equal the lock's hashes."""
    from apm_cli.cache.deploy_store import record_matches
    from apm_cli.core.scope import InstallScope
    from apm_cli.version import get_version

    records: dict[str, dict] = {}
    for key, dep in lock.dependencies.items():
        record = deploy.package_record(
            dep.repo_url,
            dep.resolved_commit,
            dep.content_hash,
            targets,
            get_version(),
            InstallScope.PROJECT,
        )
        if not record_matches(record, getattr(dep, "deployed_file_hashes", None) or {}):
            raise Decline("no matching package record")
        records[key] = record
    return records


def _plan_files(
    project_root: Path,
    lock: Any,
    records: dict[str, dict],
    deploy: Any,
    receipted: Callable[[Path], str | None],
    *,
    targets: Any,
) -> list[tuple[Path, bytes, str, str]]:
    """Missing deployed files with their exact recorded bytes; present ones must be untouched.

    A present file is APM's only when *receipted* (the ledger's trusted,
    owner- and claim-bound hash, ``DeploymentMaterializationSession.receipted_hash``)
    vouches for its locked digest; a raw receipt of the same bytes under
    another owner or claim (after a branch switch) does not.
    """
    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.utils.content_hash import canonical_digest

    prefixes = BaseIntegrator._get_integration_prefixes(targets=list(targets))
    writes: list[tuple[Path, bytes, str, str]] = []
    for owner, dep in lock.dependencies.items():
        for rel, digest in (getattr(dep, "deployed_file_hashes", None) or {}).items():
            path = _within(project_root, rel, prefixes)
            if path.is_symlink():
                raise Decline("symlinked deployed file")
            if path.exists():
                data = path.read_bytes() if path.is_file() else b""
                if not path.is_file() or f"sha256:{canonical_digest(data)}" != digest:
                    # The full run keeps bytes that differ from the lock as a
                    # local edit (it reports them), so only the full run decides.
                    raise Decline("edited deployed file")
                if receipted(path) != digest:
                    raise Decline("deployed file without a receipt")
            else:
                hexd, raw = records[owner]["files"][rel]
                data = deploy.get(hexd, raw_sha256=raw)
                if data is None:
                    raise Decline("store miss")
                writes.append((path, data, digest, owner))
            if not data.isascii() or _ESCAPE.search(data):
                # The content scanner flags only non-ASCII codepoints, which
                # escape sequences can encode: such bytes take the full run.
                raise Decline("non-ASCII deployed content")
    return writes


def _refuse_claude_carried_rules(project_root: Path, targets: Any, lock: Any = None) -> None:
    """Decline when a Claude rule may be left to a compiled AGENTS.md (#5343, #5434).

    The lock records only the rules install wrote, so a deleted carrier would
    otherwise leave Claude with no copy; the full run restores the rule. A
    locked package's routable instruction can be carried by a directory's
    AGENTS.md anywhere, so it declines too.
    """
    from apm_cli.compilation.claude_agents_md import (
        compiled_agents_md_may_carry,
        lock_ships_routable_instruction,
    )

    if not any(getattr(t, "compile_family", None) == "claude" for t in targets):
        return
    if compiled_agents_md_may_carry(project_root) or (
        lock is not None and lock_ships_routable_instruction(project_root, lock)
    ):
        raise Decline("claude rules may depend on a compiled AGENTS.md")


def _plan_packages(project_root: Path, lock: Any) -> list[tuple[Path, Path, str]]:
    from apm_cli.cache.git_cache import GitCache
    from apm_cli.cache.paths import get_cache_root
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.drift import locked_copy_is_stale
    from apm_cli.install.cache_pin import marker_is_current
    from apm_cli.install.source_helpers import locked_content_mismatch

    modules = project_root / APM_MODULES_DIR
    cache = GitCache(get_cache_root(), read_only=True)
    plan: list[tuple[Path, Path, str]] = []
    for key, dep in lock.dependencies.items():
        if key == "." or getattr(dep, "source", None) is not None:
            raise Decline("non-git source")
        if getattr(dep, "discovered_via", None) or getattr(dep, "marketplace_plugin_name", None):
            raise Decline("marketplace source")
        sha = getattr(dep, "resolved_commit", None)
        if not sha:
            raise Decline("unpinned dependency")
        if not getattr(dep, "content_hash", None):
            # locked_content_mismatch proves nothing without one: the full run decides.
            raise Decline("no locked content hash")
        ref = dep.to_dependency_ref()
        if getattr(ref, "is_virtual", False) or getattr(ref, "virtual_path", None):
            raise Decline("virtual package")
        install_path = ref.get_install_path(modules)
        if os.path.lexists(install_path) and (
            install_path.is_symlink() or not install_path.is_dir()
        ):
            # Not a package copy: APPLY never replaces it, the full run decides.
            raise Decline("non-directory at a package path")
        if install_path.is_dir():
            if not marker_is_current(install_path, sha):
                # A matching content hash does not restore a deleted or foreign
                # marker; the full run's cache-pin sync does (cache_pin.py).
                raise Decline("cache pin marker")
            # A present copy must already prove its locked content: decline before any write.
            if locked_copy_is_stale(dep, install_path) or locked_content_mismatch(
                key, dep, install_path, lockfile=lock
            ):
                raise Decline("stale package copy")
            continue
        try:
            checkout = cache.get_checkout(ref.to_github_url(), dep.resolved_ref, locked_sha=sha)
        except Exception:
            raise Decline("package not in the git cache") from None
        plan.append((checkout, install_path, sha))
    return plan


def _stage_package(checkout: Path, key: str, dep: Any, lock: Any, sha: str, staging: Path) -> Path:
    """Copy one locked package into *staging* (outside the project) and prove its content."""
    from apm_cli.cache.deploy_store import process_umask
    from apm_cli.install.cache_pin import write_marker
    from apm_cli.install.source_helpers import locked_content_mismatch

    for dirpath, dirnames, filenames in os.walk(checkout):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in (*dirnames, *filenames):
            if os.path.islink(os.path.join(dirpath, name)):
                raise Decline("symlink in a package")
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(checkout, staging, ignore=shutil.ignore_patterns(".git"))
    # The cache keeps checkouts owner-only; a materialized copy gets a normal dir mode.
    os.chmod(staging, 0o777 & ~process_umask())
    write_marker(staging, sha)
    if locked_content_mismatch(key, dep, staging, lockfile=lock):
        raise Decline("package copy differs from its locked content")
    return staging


def _publish(staging: Any, replacement: Path) -> None:
    """Activate one complete package copy: a rename inside ``apm_modules``.

    No clobber: anything that appeared at the destination since planning
    fails the publish (and the whole apply rolls back) instead of being replaced.
    """
    staging.publish_replacement(replacement, clobber=False)


def _discard_staging(staging: Path) -> None:
    """Remove the engine's own ``mkdtemp`` staging root, outside the project."""
    shutil.rmtree(staging, ignore_errors=True)


def _refuse_orphans(project_root: Path, lock: Any, receipts: dict[str, str]) -> None:
    """APPLY never deletes: any receipted output the lock no longer claims runs the full pipeline."""
    claimed = {
        (project_root / rel).resolve().as_posix()
        for dep in lock.dependencies.values()
        for rel in (getattr(dep, "deployed_file_hashes", None) or {})
    }
    if any(physical not in claimed for physical in receipts):
        raise Decline("orphaned output")


def _write(path: Path, data: bytes, scratch: Path, created: list[_Created] | None = None) -> None:
    """Create *path* with *data*, all or nothing, never replacing an existing file.

    The bytes go to a scratch file on the same filesystem first; a hard link
    (a rename on Windows, which never replaces) then makes the whole file
    appear at once, so an interrupted apply leaves no partial file behind.
    Only once it exists is it noted in *created*, with its identity and the
    directories made for it, so a rollback removes nothing it did not create.
    """
    made = []
    parent = path.parent
    while parent != parent.parent and not os.path.lexists(parent):
        made.append(parent)
        parent = parent.parent
    from apm_cli.cache.deploy_store import plain_file_mode

    path.parent.mkdir(parents=True, exist_ok=True)
    # The scratch file goes with the staging root the commit or rollback removes.
    fd, temp = tempfile.mkstemp(dir=scratch)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(temp, plain_file_mode())
    try:
        if os.name == "nt":
            os.rename(temp, path)
        else:
            os.link(temp, path)
    except OSError:
        if created is not None:
            created.append(_Created(None, None, tuple(made)))
        raise
    if created is not None:
        info = os.lstat(path)
        created.append(_Created(path, (info.st_dev, info.st_ino), tuple(made)))


@dataclass(frozen=True)
class _Created:
    """One file this apply created (``path`` None if only its directories were)."""

    path: Path | None
    identity: tuple[int, int] | None
    directories: tuple[Path, ...]


def _undo_created(created: list[_Created], root: Path) -> list[tuple[Path, str]]:
    """Remove what this apply created, newest first, only while it is still that object.

    A file someone else put at the same path (after a lost no-replace race, or
    in place of ours) is kept, and so is any directory that is no longer empty.
    A path below a directory that became a link under *root* is kept and
    reported, never removed through the link (#5207).
    """
    from apm_cli.install.deploy_snapshot import remove_empty_directories
    from apm_cli.utils.path_security import has_symlink_component

    linked = "kept: a parent directory is a symbolic link"
    issues: list[tuple[Path, str]] = []
    for entry in reversed(created):
        if entry.path is None:
            continue
        if has_symlink_component(root, entry.path.parent):
            issues.append((entry.path, linked))
            continue
        try:
            info = os.lstat(entry.path)
        except FileNotFoundError:
            continue
        if (info.st_dev, info.st_ino) != entry.identity:
            continue
        try:
            os.unlink(entry.path)
        except OSError as exc:
            issues.append((entry.path, f"could not remove: {exc}"))
    directories = {d for entry in created for d in entry.directories}
    crossing = {d for d in directories if has_symlink_component(root, d)}
    issues.extend((d, linked) for d in sorted(crossing) if os.path.lexists(d))
    remove_empty_directories(directories - crossing)
    return issues


def _audit_passes(project_root: Path, fetch: Any, *, content_integrity: bool) -> bool:
    """The ``apm audit --ci`` baseline owner on the applied state.

    Every deployed byte was just re-hashed against the lockfile and is ASCII
    (anything else declined), so the content scan cannot find anything: the
    scanner flags only non-ASCII codepoints. It is skipped as proven.
    """
    from apm_cli.install.exec_gate import exec_policy_from_discovery
    from apm_cli.policy.ci_checks import run_baseline_checks

    result = run_baseline_checks(
        project_root,
        fail_fast=True,
        ci_mode=True,
        policy_provider=lambda: exec_policy_from_discovery(lambda: fetch),
        content_integrity=content_integrity,
        # The audit's MCP view ignores personal (user-scope) approvals so CI and
        # laptops agree; install honors them. The fast run's output runs the
        # install's own MCP phase (fast_output.run_services), which reconciles,
        # parks, records and reports exactly as the full run does.
        mcp_config_consistency=False,
    )
    failing = [
        c.name
        for c in result.checks
        if c.name not in _RENDERED_BY_THE_INSTALL_OWNER
        and (not c.passed or getattr(c, "warned", False))
    ]
    if failing:
        raise Decline(f"post-apply audit: {failing[0]}")
    return True


# Audit checks the fast run hands to the install's own phase instead: the MCP
# config view of ``apm audit`` deliberately ignores personal (user-scope)
# approvals so CI and laptops agree, while ``apm install`` honors them. The
# fast run's output runs the install's MCP phase itself
# (fast_output.run_services), which reconciles, parks and reports exactly as
# the full run does.
_RENDERED_BY_THE_INSTALL_OWNER = frozenset({"config-consistency"})


# Baseline checks that fail before APPLY only because of what APPLY is about to write.
_FILLED_BY_APPLY = frozenset(
    {"deployed-files-present", "skill-subset-consistency", "config-consistency"}
)


def _audit_gate(project_root: Path, fetch: Any, *, writes: bool) -> bool:
    """Decide before any write: every baseline check passes, except ones APPLY fills.

    Returns whether a filled check failed (the applied state is then re-verified).
    """
    from apm_cli.install.exec_gate import exec_policy_from_discovery
    from apm_cli.policy.ci_checks import run_baseline_checks

    result = run_baseline_checks(
        project_root,
        fail_fast=False,
        ci_mode=True,
        policy_provider=lambda: exec_policy_from_discovery(lambda: fetch),
        content_integrity=False,
        mcp_config_consistency=False,
    )
    failing = {
        c.name
        for c in result.checks
        if c.name not in _RENDERED_BY_THE_INSTALL_OWNER
        and (not c.passed or getattr(c, "warned", False))
    }
    tolerated = _FILLED_BY_APPLY if writes else frozenset()
    if failing - tolerated:
        raise Decline(f"audit: {sorted(failing - tolerated)[0]}")
    return bool(failing)


def _refuse_unsilent_executables(lock: Any) -> None:
    """Only a silent allow hits: a gated or denied package prints the full run's lines."""
    from apm_cli.security.executables import TRUST_DENIED, TRUST_GATED

    if any(
        getattr(dep, "exec_status", None) in (TRUST_GATED, TRUST_DENIED)
        for dep in lock.dependencies.values()
    ):
        raise Decline("executables decision")


def _refuse_target_policy(project_root: Path, fetch: Any) -> None:
    """The install judges its effective targets (auto-detect included) against the
    org compilation-target policy (install/phases/policy_target_check.py); audit
    judges only declared ones. Any finding, blocking or not, takes the full run.
    """
    policy = getattr(fetch, "policy", None)
    if policy is None:
        return
    from apm_cli.core.scope import manifest_file
    from apm_cli.core.target_detection import resolve_effective_target_decision
    from apm_cli.install.phases.policy_target_check import TARGET_CHECK_IDS
    from apm_cli.models.apm_package import APMPackage, package_target_selection
    from apm_cli.policy.policy_checks import run_dependency_policy_checks

    manifest = APMPackage.from_apm_yml(manifest_file(project_root))
    decision = resolve_effective_target_decision(
        project_root,
        explicit_target=None,
        manifest_target=package_target_selection(manifest),
        create_config=False,  # deciding never writes APM_HOME/config.json
    )
    if decision.canonical_targets is None:
        return
    result = run_dependency_policy_checks(
        [],
        policy=policy,
        effective_target=decision.canonical_targets,
        effective_target_source=decision.source,
        fetch_outcome=getattr(fetch, "outcome", None),
        fail_fast=False,
    )
    if any(c.name in TARGET_CHECK_IDS and not c.passed for c in result.checks):
        raise Decline("target policy")


def _refuse_lifecycle_scripts(project_root: Path) -> None:
    """Lifecycle scripts (policy, user or project) run only inside the full install."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    if discover_scripts(str(project_root)):
        raise Decline("lifecycle scripts")


def _refuse_native_plugins(project_root: Path, lock: Any) -> None:
    """Native plugin registration and retirement run only in the full install's
    last phases (install/pipeline.py: codex_plugins, claude_plugins), which APPLY omits.
    """
    from apm_cli.codex_plugins.registrar import ledger_path_for
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.models.validation import PackageType
    from apm_cli.native_plugins.record import lock_native_plugins

    if lock_native_plugins(lock):
        raise Decline("native plugin")
    if os.path.lexists(ledger_path_for(project_root / APM_MODULES_DIR)):
        # Codex registrations APM owns: the full run refreshes or retires them.
        raise Decline("codex native registrations")
    from apm_cli.native_plugins.claude_marketplaces import project_release_pending

    if project_release_pending(project_root):
        # A Claude registry entry a failed uninstall left: the full run releases it (#5258).
        raise Decline("claude marketplace registration")
    plugins = {PackageType.AGENT_PLUGIN.value, PackageType.MARKETPLACE_PLUGIN.value}
    if any(getattr(dep, "package_type", None) in plugins for dep in lock.dependencies.values()):
        raise Decline("plugin package")


def _refuse_legacy_skill_layout(project_root: Path, lock: Any) -> None:
    """The skill layout is not in the deploy record: a legacy request or a pending
    legacy-path migration (install/skill_path_migration.py) takes the full run.
    """
    from apm_cli.install.skill_path_migration import detect_legacy_skill_deployments
    from apm_cli.integration.targets import should_use_legacy_skill_paths

    if should_use_legacy_skill_paths():
        raise Decline("legacy skill paths")
    if detect_legacy_skill_deployments(lock, project_root):
        raise Decline("legacy skill migration pending")


def _stage_all(
    project_root: Path, lock: Any, packages: list, stage_root: Path
) -> list[tuple[Path, Path]]:
    """Stage every missing package copy under *stage_root* in the apm_modules layout."""
    from apm_cli.constants import APM_MODULES_DIR

    modules = project_root / APM_MODULES_DIR
    staged: list[tuple[Path, Path]] = []
    for checkout, install_path, sha in packages:
        key = next(
            k
            for k, d in lock.dependencies.items()
            if _install_path(project_root, d) == install_path
        )
        target = stage_root / APM_MODULES_DIR / install_path.relative_to(modules)
        staged.append(
            (_stage_package(checkout, key, lock.dependencies[key], lock, sha, target), install_path)
        )
    return staged


def _verify_staged(
    project_root: Path, lock: Any, stage_root: Path, targets: Any, fetch: Any, any_staged: bool
) -> None:
    """The pre-deploy scan and the executables decision see staged copies as well."""
    if not any_staged:
        return
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.core.scope import manifest_file
    from apm_cli.install import noop_diff
    from apm_cli.install.exec_gate import exec_policy_from_discovery, stale_exec_decisions

    if not noop_diff.sources_scan_clean(stage_root, lock):
        raise Decline("security scan findings")
    stale, indeterminate = stale_exec_decisions(
        lock,
        stage_root / APM_MODULES_DIR,
        manifest_file(project_root),
        policy_provider=lambda: exec_policy_from_discovery(lambda: fetch),
        targets=list(targets),
        first_only=True,
    )
    if stale or indeterminate:
        raise Decline("executables decision")


def _commit(
    project_root: Path,
    fetch: Any,
    staged: list[tuple[Path, Path]],
    files: list,
    targets: Any,
    session: Any,
    recheck: bool,
    render: Any,
) -> Any:
    """Create the decided state and print it inside one journal.

    Each file this apply creates is noted only once it exists, with its
    identity; the journal records, at the write seam, every path the receipts,
    ``.gitignore`` and service owners write while the output renders. The output is held until the journal settles. Any failure
    restores every path and drops the output, so the full pipeline runs from
    the pre-run tree and prints its own outcome once.
    """
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.deps.lockfile import get_lockfile_path
    from apm_cli.install.fast_output import held_output, quiet
    from apm_cli.install.resolution_staging import ResolutionStagingSession
    from apm_cli.install.transaction import ApplyRestoreIncomplete, journaled_apply

    modules = project_root / APM_MODULES_DIR
    # Package copies and file bytes are staged inside apm_modules, on the
    # project's filesystem, so each one appears by a single rename or link.
    # An interrupted run leaves only an inactive staging root the next
    # install removes (install/resolution_staging.py).
    staging = ResolutionStagingSession(modules)
    created: list[_Created] = []
    try:
        with held_output() as held, journaled_apply(project_root) as journal:
            # The services phase may rewrite the lock outside the write seam.
            journal.record(get_lockfile_path(project_root))
            # A killed run's inactive staging roots go silently, as at the full
            # run's commit; a cleanup that fails is reported there, so it decides.
            if staging.remove_abandoned_roots():
                raise Decline("abandoned staging cleanup failed")
            scratch = staging.prepare_replacement(modules / _SCRATCH)
            scratch.mkdir()
            replacements = []
            for source, install_path in staged:
                replacement = staging.prepare_replacement(install_path)
                shutil.copytree(source, replacement, symlinks=True)
                replacements.append(replacement)
            for replacement in replacements:
                _publish(staging, replacement)
            for path, data, _digest, _owner in files:
                _write(path, data, scratch, created)
            if recheck:
                with quiet():
                    _audit_passes(project_root, fetch, content_integrity=False)
            by_owner: dict[str, dict[Path, str]] = {}
            for path, _data, digest, owner in files:
                by_owner.setdefault(owner, {})[path] = digest
            for owner, written in by_owner.items():
                session.record(written, targets, owner)
            session.flush()
            render()
    except BaseException as exc:
        # The journal has put every deployed path back; the packages go next.
        prior = exc.issues if isinstance(exc, ApplyRestoreIncomplete) else []
        issues = [*prior, *_undo_created(created, project_root), *staging.rollback()]
        if issues:
            raise ApplyRestoreIncomplete(issues) from exc
        if isinstance(exc, Decline) or not isinstance(exc, Exception):
            raise  # an interrupt is restored, then propagates
        raise Decline(f"write failed ({type(exc).__name__})") from exc
    staging.commit()
    # Scratch staging alone must not leave an apm_modules the full run would
    # not create, e.g. for a project with only MCP servers (#5159, I2).
    staging.remove_created_modules_dir()
    return held


def try_apply(
    options: dict[str, Any],
    argv: list[str] | None,
    project_root: Path,
    *,
    frozen_disposition: Any,
    decide_only: bool = False,
) -> bool:
    """Apply the verified diff and return True; False (with a trace reason) runs the full pipeline."""
    from apm_cli.install import noop_diff
    from apm_cli.install.transaction import ApplyRestoreIncomplete

    if not noop_diff.eligible(options, argv):
        _trace("full run (options)")
        return False
    import time

    started = time.perf_counter()
    try:
        from apm_cli.deps.egress_route import EgressSurface, is_proxy_only
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path, installed_apm_version
        from apm_cli.deps.registry_proxy import RegistryConfig

        if RegistryConfig.from_env() is not None or is_proxy_only(EgressSurface.SOURCE_ARCHIVE):
            # Source routing (and PROXY_REGISTRY_ONLY's lockfile refusal,
            # install/pipeline.py) is the full pipeline's egress decision.
            raise Decline("registry proxy configured")
        from apm_cli.core.scope import get_apm_home, get_workspace_deploy_root
        from apm_cli.install.deploy_snapshot import has_abandoned_journals

        root = get_workspace_deploy_root(project_root)  # replay's identity (install/transaction.py)
        if has_abandoned_journals(get_apm_home() / "install-snapshots", root, project_root):
            # The full run replays this workspace's journal first, under the lifecycle lock (#4863).
            raise Decline("interrupted install journal")
        import click

        from apm_cli.deps.lockfile_generator import require_current_cli
        from apm_cli.install import verdict_attest

        # An attested record MACs these exact lock bytes and this build, which
        # passed the generator gate when it committed them: the gate's answer is
        # the same, so only a run without a matching attestation re-reads it.
        attested = verdict_attest.attested_state(list(argv or []), project_root)
        try:
            if not attested:
                # The command's own gate: the full run renders its refusal.
                require_current_cli(project_root, command="apm install")
        except click.ClickException:
            raise Decline("lockfile generator") from None
        lock_path = get_lockfile_path(project_root)
        lock = LockFile.read(lock_path)
        if lock is None:
            raise Decline("no lockfile")
        if lock.apm_version != installed_apm_version():
            raise Decline("lockfile from another apm version")
        if lock.lsp_servers or lock.local_deployed_files:
            raise Decline("local content or LSP")
        if (project_root / ".apm").exists():
            raise Decline("root project primitives")
        _refuse_native_plugins(project_root, lock)
        _refuse_legacy_skill_layout(project_root, lock)
        from apm_cli.core.experimental import is_enabled

        if is_enabled("external_scanners"):
            # The opt-in install-time audit phase scans every install.
            raise Decline("install-time audit enabled")
        from apm_cli.install.fast_output import quiet

        with quiet():
            admits, fetch = noop_diff._policy_admits(project_root)
        if not admits:
            raise Decline("policy")
        with quiet():
            _refuse_target_policy(project_root, fetch)
        if not noop_diff.sources_scan_clean(project_root, lock):
            raise Decline("security scan findings")
        from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
        from apm_cli.core.scope import InstallScope
        from apm_cli.policy.ci_checks import resolve_audit_scope

        _root, user_scope, targets = resolve_audit_scope(project_root, lock)
        if user_scope or not targets:
            raise Decline("scope")
        _refuse_claude_carried_rules(project_root, targets, lock)
        from apm_cli.cache.deploy_store import DeployStore

        session = DeploymentMaterializationSession(project_root, InstallScope.PROJECT, lock)
        deploy = DeployStore.open_default()
        if deploy is None:
            raise Decline("no deploy store")
        if attested and verdict_attest.attested_gates(project_root, lock, fetch):
            if decide_only:
                _trace("no-op (attested record)")
                return True
            raise _AttestedCommitted(
                _commit_attested(
                    project_root, lock, fetch, targets, session, started, frozen_disposition
                )
            )
        packages = _plan_packages(project_root, lock)
        receipts = session.receipt_hashes()
        _refuse_orphans(project_root, lock, receipts)
        records = _package_records(lock, targets, deploy)
        files = _plan_files(
            project_root,
            lock,
            records,
            deploy,
            lambda path: session.receipted_hash(path, targets),
            targets=targets,
        )
        outcomes = _outcomes(lock, files, records)
        _refuse_lifecycle_scripts(project_root)
        _refuse_unsilent_executables(lock)
        with quiet():
            recheck = _audit_gate(project_root, fetch, writes=bool(files or packages))
            if not noop_diff._policy_checks_pass(project_root, fetch):
                raise Decline("policy checks")
        stage_root = Path(tempfile.mkdtemp(prefix="apm-fast-"))
        try:
            staged = _stage_all(project_root, lock, packages, stage_root)
            _refuse_unproven_edges(project_root, lock, staged)
            _verify_staged(project_root, lock, stage_root, targets, fetch, bool(packages))
            if decide_only:
                # Test seam (APM_INSTALL_FAST_PATH=decide): report the decision, write nothing.
                _trace(
                    f"applied a verified diff ({len(files)} files)"
                    if files or packages
                    else "no-op (desired state already deployed)"
                )
                return True

            def render() -> None:
                _render(project_root, lock, outcomes, packages, files, started, frozen_disposition)

            # The decision is final: nothing above wrote to the project or HOME.
            held = _commit(project_root, fetch, staged, files, targets, session, recheck, render)
        finally:
            _discard_staging(stage_root)
    except Decline as exc:
        _trace(f"full run ({exc})")
        return False
    except ApplyRestoreIncomplete:
        raise  # the tree is not the pre-run tree: never run the full pipeline on it
    except _AttestedCommitted as committed:
        # Committed: as below, the held output is printed outside the fallback.
        return verdict_attest.release_attested(committed.args[0])
    except Exception as exc:
        _trace(f"full run (error: {type(exc).__name__})")
        return False
    # Committed: the run is this apply. Printing its held output is outside the
    # fallback, so a failure here propagates and never starts the full pipeline.
    held.release()
    _trace(
        f"applied a verified diff ({len(files)} files)"
        if files or packages
        else "no-op (desired state already deployed)"
    )
    if not (files or packages):
        from apm_cli.install import verdict_record

        # An applied diff holds its service output, so only a no-op is recorded (#4977).
        verdict_record.note_verified_noop()
    return True


class _AttestedCommitted(Exception):
    """An attested no-op committed its journal (``args[0]`` holds its output)."""


def _commit_attested(
    project_root: Path,
    lock: Any,
    fetch: Any,
    targets: Any,
    session: Any,
    started: float,
    frozen_disposition: Any,
) -> Any:
    """Render the no-op through the proven no-op's own path: an empty diff, journaled."""

    def render() -> None:
        _render(project_root, lock, [], [], [], started, frozen_disposition)

    return _commit(project_root, fetch, [], [], targets, session, False, render)


def _render(
    project_root: Path,
    lock: Any,
    outcomes: list,
    packages: list,
    files: list,
    started: float,
    frozen_disposition: Any,
) -> None:
    """Print the full run's lines through its own owners (install/fast_output.py)."""
    from apm_cli.core.scope import manifest_file
    from apm_cli.install import fast_output
    from apm_cli.models.apm_package import APMPackage

    copied = {str(install_path) for _c, install_path, _s in packages}
    fetched = frozenset(
        o.key for o in outcomes if str(_install_path(project_root, o.dep)) in copied
    )
    fast_output.render(
        project_root,
        lock,
        APMPackage.from_apm_yml(manifest_file(project_root)),
        started=started,
        bodies={o.key: _body_printer(o.body) for o in outcomes} if (files or packages) else None,
        installed=sum(1 for o in outcomes if o.files_written or o.key in fetched),
        frozen_disposition=frozen_disposition,
        fetched=fetched,
    )


def _body_printer(body: tuple[str, ...]) -> Any:
    def emit(logger: Any) -> None:
        for line in body:
            logger.tree_item(line)

    return emit


def _refuse_unproven_edges(project_root: Path, lock: Any, staged: list[tuple[Path, Path]]) -> None:
    """Every declared dependency edge must be satisfied by the lock, as the frozen resolver proves.

    Walks the root manifest (prod and dev) and each locked package's manifest
    (its verified copy: present, or staged this run) and admits each edge
    through the resolver's owner, ``ImmutableRequirements`` in frozen mode,
    without a remote resolver: a ref the lock cannot prove declines.
    """
    from apm_cli.core.scope import manifest_file
    from apm_cli.deps.dependency_graph import DependencyNode
    from apm_cli.deps.immutable_requirements import (
        ImmutableRequirementError,
        ImmutableRequirements,
    )
    from apm_cli.models.apm_package import APMPackage

    root = APMPackage.from_apm_yml(manifest_file(project_root))
    if root.replace_rules:
        raise Decline("replacement rules")
    requirements = ImmutableRequirements(root.name, None, lock, frozen=True)
    copies = {install_path: copy for copy, install_path in staged}
    queue = [
        (dep, None) for dep in (*root.get_apm_dependencies(), *root.get_dev_apm_dependencies())
    ]
    visited: set[str] = set()
    while queue:
        dep, parent = queue.pop(0)
        if dep.is_parent_repo_inheritance or dep.is_marketplace or dep.is_local:
            raise Decline("unproven dependency edge")
        node = DependencyNode(
            package=APMPackage(name=dep.get_display_name(), version="unknown"),
            dependency_ref=dep,
            depth=1 if parent is None else parent.depth + 1,
            parent=parent,
        )
        try:
            requirements.add(node)
        except ImmutableRequirementError:
            raise Decline("immutable requirement not proven by the lock") from None
        key = dep.get_resolution_key()
        if key in visited:
            continue
        visited.add(key)
        locked = lock.get_dependency(dep.get_unique_key())
        if locked is None:
            raise Decline("unlocked dependency edge")
        install_path = _install_path(project_root, locked)
        package_dir = copies.get(install_path, install_path)
        if not manifest_file(package_dir).is_file():
            continue
        try:
            package = APMPackage.from_apm_yml(manifest_file(package_dir), source_path=package_dir)
        except (OSError, ValueError):
            raise Decline("unreadable package manifest") from None
        queue.extend((child, node) for child in package.get_apm_dependencies())


def _install_path(project_root: Path, dep: Any) -> Path:
    from apm_cli.constants import APM_MODULES_DIR

    return dep.to_dependency_ref().get_install_path(project_root / APM_MODULES_DIR)
