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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_STORE_DIR = "deploy-store-v1"
_EXTRAS_DIR = "extras"
# Project files APM writes beside the deployed primitives; the lock does not hash them.
_SKIP_HARVEST = {"apm.yml", "apm.lock.yaml"}
_SKIP_DIRS = {"apm_modules", ".git"}


# Per-package integration body lines this process emitted (full run), by dependency key.
_BODIES: dict[str, list[str]] = {}


def package_body(package_name: str) -> list[str]:
    """The list one full-run package integration appends its tree lines to."""
    body: list[str] = []
    _BODIES[str(package_name)] = body
    return body


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


def _manifest_bytes(project_root: Path) -> bytes:
    from apm_cli.core.scope import manifest_file

    return manifest_file(project_root).read_bytes()


def _extras_key(lock_text: str, manifest: bytes) -> str:
    """Digest of what renders the unhashed configs: the lock's MCP sections and the manifest."""
    lines = lock_text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("mcp_")), len(lines))
    head = [line for line in lines if line.startswith(("apm_version:", "lockfile_version:"))]
    payload = "\n".join([*head, *lines[start:]]).encode() + b"\0" + manifest
    return hashlib.sha256(payload).hexdigest()


def _deploy_tops() -> set[str]:
    """First path components any known target deploys under (bounds the snapshot walk)."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    tops: set[str] = set()
    for target in KNOWN_TARGETS.values():
        roots = [
            target.root_dir,
            *(m.deploy_root for m in target.primitives.values() if m.deploy_root),
        ]
        tops.update(str(r).split("/", 1)[0] for r in roots if r)
    return tops - _SKIP_DIRS


def _snapshot(root: Path) -> dict[str, str]:
    """Hashes of root-level files and of files under target deploy roots only."""
    out: dict[str, str] = {}
    tops = _deploy_tops()
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and entry.name not in _SKIP_HARVEST:
                out[entry.name] = _sha256(Path(entry.path).read_bytes())
    for top in sorted(tops):
        base = root / top
        if not base.is_dir() or base.is_symlink():
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                path = Path(dirpath, name)
                if not path.is_symlink():
                    out[path.relative_to(root).as_posix()] = _sha256(path.read_bytes())
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
        hashed = {
            rel
            for dep in lock.dependencies.values()
            for rel in (getattr(dep, "deployed_file_hashes", None) or {})
        }
        if before is None:
            return
        after = _snapshot(project_root)
        extras = []
        for rel, digest in sorted(after.items()):
            if rel in hashed or before.get(rel) == digest:
                continue
            data = (project_root / rel).read_bytes()
            extras.append([rel, before.get(rel), _store_put(store, data)])
        key = _extras_key(lock_path.read_text(encoding="utf-8"), _manifest_bytes(project_root))
        index = store / _EXTRAS_DIR / f"{key}.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=index.parent, prefix=".idx.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(extras, handle)
        os.replace(tmp, index)
    except Exception as exc:  # the store is an accelerator; never fail an install
        _trace(f"harvest skipped ({type(exc).__name__})")


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
    project_root: Path, lock: Any, records: dict[str, dict], deploy: Any, receipts: dict[str, str]
) -> list[tuple[Path, bytes, str, str]]:
    """Missing deployed files with their exact recorded bytes; present ones must be untouched."""
    from apm_cli.utils.content_hash import canonical_digest

    writes: list[tuple[Path, bytes, str, str]] = []
    for owner, dep in lock.dependencies.items():
        for rel, digest in (getattr(dep, "deployed_file_hashes", None) or {}).items():
            path = _within(project_root, rel)
            if path.is_symlink():
                raise Decline("symlinked deployed file")
            if path.exists():
                data = path.read_bytes() if path.is_file() else b""
                if not path.is_file() or f"sha256:{canonical_digest(data)}" != digest:
                    raise Decline("edited deployed file")
                if receipts.get(path.resolve().as_posix()) != digest:
                    raise Decline("deployed file without a receipt")
            else:
                hexd, raw = records[owner]["files"][rel]
                data = deploy.get(hexd, raw_sha256=raw)
                if data is None:
                    raise Decline("store miss")
                writes.append((path, data, digest, owner))
            if not data.isascii():
                # The content scanner flags only non-ASCII codepoints: such bytes take the full run.
                raise Decline("non-ASCII deployed content")
    return writes


def _plan_extras(project_root: Path, lock_text: str, store: Path) -> list[tuple[Path, bytes]]:
    key = _extras_key(lock_text, _manifest_bytes(project_root))
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
        dirnames[:] = [d for d in dirnames if d != ".git"]
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
    )
    return result.passed and not any(getattr(c, "warned", False) for c in result.checks)


def try_apply(
    options: dict[str, Any],
    argv: list[str] | None,
    project_root: Path,
    *,
    frozen_disposition: Any,
) -> bool:
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
        admits, fetch = noop_diff._policy_admits(project_root)
        if not admits:
            raise Decline("policy")
        from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
        from apm_cli.core.scope import InstallScope
        from apm_cli.policy.ci_checks import resolve_audit_scope

        _root, user_scope, targets = resolve_audit_scope(project_root, lock)
        if user_scope or not targets:
            raise Decline("scope")
        from apm_cli.cache.deploy_store import DeployStore

        session = DeploymentMaterializationSession(project_root, InstallScope.PROJECT, lock)
        deploy = DeployStore.open_default()
        if deploy is None:
            raise Decline("no deploy store")
        store = _store_root()
        packages = _plan_packages(project_root, lock)
        receipts = session.receipt_hashes()
        _refuse_orphans(project_root, lock, receipts)
        records = _package_records(lock, targets, deploy)
        files = _plan_files(project_root, lock, records, deploy, receipts)
        outcomes = _outcomes(lock, files, records)
        extras = _plan_extras(project_root, lock_path.read_text(encoding="utf-8"), store)
        if not all(data.isascii() for _p, data in extras):
            raise Decline("non-ASCII deployed content")
        for checkout, install_path, sha in packages:
            _copy_package(checkout, install_path, sha)
        for path, data, _d, _o in files:
            _write(path, data)
        for path, data in extras:
            _write(path, data)
        if not (
            noop_diff._copies_valid(project_root, lock)
            and _audit_passes(project_root, fetch, content_integrity=False)
            and noop_diff._policy_checks_pass(project_root, fetch)
        ):
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
    _trace(f"applied a verified diff ({count} files)")
    try:
        from apm_cli.install.fast_output import render
    except ImportError:
        render = None
    if render is not None:
        render(project_root, lock, outcomes, frozen_disposition=frozen_disposition, started=started)
        return True
    from apm_cli.utils.console import _rich_info, _rich_success

    if not (count or packages):
        elapsed = time.perf_counter() - started
        _rich_info(
            f"No changes -- install state already up to date in {elapsed:.1f}s.", symbol="info"
        )
        return True

    _rich_success(
        f"Installed {len(lock.dependencies)} locked dependencies from the cache "
        f"({len(packages)} packages, {count} files).",
        symbol="success",
    )
    return True
