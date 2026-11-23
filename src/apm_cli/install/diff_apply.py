"""Diff-apply install (lane C, S2): materialize a locked project from the caches.

A ``--frozen`` or locked re-install of a project whose lockfile is current needs
no resolve and no rendering: the lockfile already names every package commit,
its content hash and every deployed file's hash. This module computes the diff
between that desired state and the disk, and fills it from the caches:

- package copies come from the read-only git cache (pinned commit, verified
  HEAD), are copied without ``.git`` and must then prove their locked content;
- deployed files come from the deploy store (sha256 -> bytes), each re-hashed
  against the lockfile hash before it is written;
- APM-written configs the lockfile does not hash (``.mcp.json``,
  ``.gitignore``) come from a store index keyed by the lockfile sections that
  render them and by the file's previous bytes.

The org policy gate is discovered live, and the ``apm audit --ci`` baseline
owner must pass on the result (deployed hashes, MCP config consistency, hook
approval pins and the executables gate). Anything uncertain (a store miss, an
edited deployed file, a symlink in a package, local or registry sources, any
option) declines, and the full pipeline runs. ``APM_INSTALL_FAST_PATH=0`` turns
it off; ``APM_INSTALL_FAST_PATH=trace`` prints one decision line.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_STORE_DIR = "deploy-store-v1"
_EXTRAS_DIR = "extras"
# Project files APM writes beside the deployed primitives; the lock does not hash them.
_SKIP_HARVEST = {"apm.yml", "apm.lock.yaml"}
_SKIP_DIRS = {"apm_modules", ".git", ".apm-local"}


class Decline(Exception):
    """The fast path cannot prove the result: run the full pipeline."""


def _trace(message: str) -> None:
    if os.environ.get("APM_INSTALL_FAST_PATH", "").strip().lower() == "trace":
        sys.stderr.write(f"[i] install fast path: {message}\n")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _store_root() -> Path:
    from apm_cli.cache.paths import get_cache_root

    return get_cache_root() / _STORE_DIR


def _blob_path(store: Path, digest: str) -> Path:
    hexd = digest.split(":", 1)[1]
    return store / "blobs" / hexd[:2] / hexd


def _store_get(store: Path, digest: str) -> bytes:
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise Decline("non-canonical hash")
    try:
        data = _blob_path(store, digest).read_bytes()
    except OSError:
        raise Decline("store miss") from None
    if _sha256(data) != digest:
        raise Decline("tampered store entry")
    return data


def _store_put(store: Path, data: bytes) -> str:
    digest = _sha256(data)
    path = _blob_path(store, digest)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".blob.")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    return digest


def _extras_key(lock_text: str, manifest: bytes) -> str:
    """Digest of what renders the unhashed configs: the lock's MCP sections and the manifest."""
    lines = lock_text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("mcp_")), len(lines))
    head = [line for line in lines if line.startswith(("apm_version:", "lockfile_version:"))]
    payload = "\n".join([*head, *lines[start:]]).encode() + b"\0" + manifest
    return hashlib.sha256(payload).hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.samefile(dirpath, root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if rel in _SKIP_HARVEST:
                continue
            out[rel] = _sha256(path.read_bytes())
    return out


def snapshot(project_root: Path) -> dict[str, str] | None:
    """Project file hashes before a full run (``harvest`` diffs against them)."""
    try:
        return _snapshot(project_root)
    except OSError:
        return None


def harvest(project_root: Path, before: dict[str, str] | None) -> None:
    """After a successful full run: store every locked deployed file and the extra configs."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    try:
        lock_path = get_lockfile_path(project_root)
        lock = LockFile.read(lock_path)
        if lock is None:
            return
        store = _store_root()
        hashed: set[str] = set()
        for dep in lock.dependencies.values():
            for rel, digest in (getattr(dep, "deployed_file_hashes", None) or {}).items():
                hashed.add(rel)
                path = project_root / rel
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()
                    if _sha256(data) == digest:
                        _store_put(store, data)
        if before is None:
            return
        after = _snapshot(project_root)
        extras = []
        for rel, digest in sorted(after.items()):
            if rel in hashed or before.get(rel) == digest:
                continue
            data = (project_root / rel).read_bytes()
            extras.append([rel, before.get(rel), _store_put(store, data)])
        key = _extras_key(
            lock_path.read_text(encoding="utf-8"), (project_root / "apm.yml").read_bytes()
        )
        index = store / _EXTRAS_DIR / f"{key}.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=index.parent, prefix=".idx.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(extras, handle)
        os.replace(tmp, index)
    except Exception as exc:  # the store is an accelerator; never fail an install
        _trace(f"harvest skipped ({type(exc).__name__})")


def _within(root: Path, rel: str) -> Path:
    if (
        not rel
        or rel.startswith("/")
        or "\\" in rel
        or any(p in ("", ".", "..") for p in rel.split("/"))
    ):
        raise Decline(f"unsafe path {rel!r}")
    path = root / rel
    current = root
    for part in rel.split("/")[:-1]:
        current = current / part
        if current.is_symlink():
            raise Decline("symlink in a deployed path")
    return path


def _plan_files(
    project_root: Path, lock: Any, store: Path, ascii_ok: list[bool], receipts: dict[str, str]
) -> list[tuple[Path, bytes, str, str]]:
    writes: list[tuple[Path, bytes, str, str]] = []
    for owner, dep in lock.dependencies.items():
        for rel, digest in (getattr(dep, "deployed_file_hashes", None) or {}).items():
            path = _within(project_root, rel)
            if path.is_symlink():
                raise Decline("symlinked deployed file")
            if path.exists():
                data = path.read_bytes() if path.is_file() else b""
                if not path.is_file() or _sha256(data) != digest:
                    raise Decline("edited deployed file")
                if receipts.get(path.resolve().as_posix()) != digest:
                    raise Decline("deployed file without a receipt")
                ascii_ok[0] = ascii_ok[0] and data.isascii()
                continue
            data = _store_get(store, digest)
            ascii_ok[0] = ascii_ok[0] and data.isascii()
            writes.append((path, data, digest, owner))
    return writes


def _plan_extras(project_root: Path, lock_text: str, store: Path) -> list[tuple[Path, bytes]]:
    key = _extras_key(lock_text, (project_root / "apm.yml").read_bytes())
    try:
        extras = json.loads((store / _EXTRAS_DIR / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Decline("no extras index") from None
    writes: list[tuple[Path, bytes]] = []
    for rel, before, after in extras:
        path = _within(project_root, rel)
        current = _sha256(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
        if path.is_symlink() or (path.exists() and current is None):
            raise Decline("unexpected config entry")
        if current == after:
            continue
        if current != before:
            raise Decline(f"config {rel} differs from the recorded baseline")
        writes.append((path, _store_get(store, after)))
    return writes


def _plan_packages(project_root: Path, lock: Any) -> list[tuple[Path, Path, str]]:
    from apm_cli.cache.git_cache import GitCache
    from apm_cli.cache.paths import get_cache_root
    from apm_cli.constants import APM_MODULES_DIR

    modules = project_root / APM_MODULES_DIR
    cache = GitCache(get_cache_root(), read_only=True)
    plan: list[tuple[Path, Path, str]] = []
    for key, dep in lock.dependencies.items():
        if key == "." or getattr(dep, "source", None) is not None:
            raise Decline("non-git source")
        sha = getattr(dep, "resolved_commit", None)
        if not sha:
            raise Decline("unpinned dependency")
        ref = dep.to_dependency_ref()
        if getattr(ref, "is_virtual", False) or getattr(ref, "virtual_path", None):
            raise Decline("virtual package")
        install_path = ref.get_install_path(modules)
        if install_path.is_dir():
            continue
        try:
            checkout = cache.get_checkout(ref.to_github_url(), dep.resolved_ref, locked_sha=sha)
        except Exception:
            raise Decline("package not in the git cache") from None
        plan.append((checkout, install_path, sha))
    return plan


def _copy_package(checkout: Path, install_path: Path, sha: str) -> None:
    from apm_cli.install.cache_pin import write_marker

    for dirpath, dirnames, filenames in os.walk(checkout):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in (*dirnames, *filenames):
            if os.path.islink(os.path.join(dirpath, name)):
                raise Decline("symlink in a package")
    install_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=install_path.parent, prefix=f".{install_path.name}."))
    try:
        shutil.copytree(
            checkout, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git")
        )
        os.replace(staging, install_path)
    except BaseException:
        _discard_staging(staging)
        raise
    write_marker(install_path, sha)


def _discard_staging(staging: Path) -> None:
    """The only removal on this path: the engine's own fresh ``mkdtemp`` staging dir."""
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


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _audit_passes(project_root: Path, fetch: Any, *, content_integrity: bool) -> bool:
    """The ``apm audit --ci`` baseline owner on the applied state.

    Every deployed byte was just re-hashed against the lockfile; when all of it
    is ASCII the content scan cannot find anything (the scanner flags only
    non-ASCII codepoints), so it is skipped as proven.
    """
    from apm_cli.install.exec_gate import exec_policy_from_discovery
    from apm_cli.policy.ci_checks import run_baseline_checks

    result = run_baseline_checks(
        project_root,
        fail_fast=True,
        ci_mode=True,
        policy_provider=lambda: exec_policy_from_discovery(lambda: fetch),
        content_integrity=content_integrity,
    )
    return result.passed and not any(getattr(c, "warned", False) for c in result.checks)


def try_apply(options: dict[str, Any], argv: list[str] | None, project_root: Path) -> bool:
    """Apply the verified diff and return True; False (with a trace reason) runs the full pipeline."""
    from apm_cli.install import noop_diff

    if not noop_diff.eligible(options, argv):
        _trace("full run (options)")
        return False
    import time

    started = time.perf_counter()
    try:
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path, installed_apm_version

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
        from apm_cli.install.fast_output import quiet

        with quiet():
            admits, fetch = noop_diff._policy_admits(project_root)
        if not admits:
            raise Decline("policy")
        if not noop_diff.sources_scan_clean(project_root, lock):
            raise Decline("security scan findings")
        from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
        from apm_cli.core.scope import InstallScope
        from apm_cli.policy.ci_checks import resolve_audit_scope

        _root, user_scope, targets = resolve_audit_scope(project_root, lock)
        if user_scope or not targets:
            raise Decline("scope")
        session = DeploymentMaterializationSession(project_root, InstallScope.PROJECT, lock)
        store = _store_root()
        packages = _plan_packages(project_root, lock)
        ascii_ok = [True]
        receipts = session.receipt_hashes()
        _refuse_orphans(project_root, lock, receipts)
        files = _plan_files(project_root, lock, store, ascii_ok, receipts)
        extras = _plan_extras(project_root, lock_path.read_text(encoding="utf-8"), store)
        ascii_ok[0] = ascii_ok[0] and all(data.isascii() for _p, data in extras)
        for checkout, install_path, sha in packages:
            _copy_package(checkout, install_path, sha)
        for path, data, _d, _o in files:
            _write(path, data)
        for path, data in extras:
            _write(path, data)
        with quiet():
            verified = (
                noop_diff._copies_valid(project_root, lock)
                and _audit_passes(project_root, fetch, content_integrity=not ascii_ok[0])
                and noop_diff._policy_checks_pass(project_root, fetch)
            )
        if not verified:
            raise Decline("post-apply audit")
        by_owner: dict[str, dict[Path, str]] = {}
        for path, _data, digest, owner in files:
            by_owner.setdefault(owner, {})[path] = digest
        for owner, written in by_owner.items():
            session.record(written, targets, owner)
        session.flush()
    except Decline as exc:
        _trace(f"full run ({exc})")
        return False
    except Exception as exc:
        _trace(f"full run (error: {type(exc).__name__})")
        return False
    count = len(files) + len(extras)
    _trace(
        f"applied a verified diff ({count} files)"
        if count or packages
        else "no-op (desired state already deployed)"
    )
    from apm_cli.utils.console import _rich_success

    if not (count or packages):
        # The empty diff prints the full no-op's lines through the same owners.
        from apm_cli.core.scope import manifest_file
        from apm_cli.install import fast_output
        from apm_cli.models.apm_package import APMPackage

        manifest = APMPackage.from_apm_yml(manifest_file(project_root))
        fast_output.render(
            project_root, lock, manifest, started=started, frozen=bool(options.get("frozen"))
        )
        return True

    _rich_success(
        f"Installed {len(lock.dependencies)} locked dependencies from the cache "
        f"({len(packages)} packages, {count} files).",
        symbol="success",
    )
    return True
