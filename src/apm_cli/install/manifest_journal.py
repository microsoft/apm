"""Crash recovery of the ``apm.yml`` an ``apm install <package>`` changed (#5454).

``apm install ./vendor/new`` writes the new entry to ``apm.yml`` before its
lockfile commits. :class:`~apm_cli.install.transaction.InstallTransaction`
restores the manifest in process on a failure or an interrupt, but a process
killed in between (SIGKILL, OOM, power loss) left ``apm.yml`` declaring a
package the lock has no row for, and ``apm install --frozen`` refused it.

Before the attempt changes ``apm.yml`` the transaction writes one record per
workspace under the APM home ``install-snapshots`` directory: the prior bytes
(or that there were none), the lockfile on disk, the hash of each manifest the
attempt wrote (a seal), and the lockfile it is about to commit (a mark). The
record is dropped once that lockfile is on disk or the transaction ends in
process. The next install entry, under the lifecycle lock and before it reads
the manifest, replays a record a killed process left
(:func:`replay_abandoned_manifest`): while the lockfile is the one the killed
install found and ``apm.yml`` holds bytes it wrote, the prior manifest is put
back atomically; anything else is kept and named.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any

_FORMAT = "apm-manifest-journal"
_VERSION = 1
_PREFIX = "manifest-"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_path(workspace: Path) -> Path:
    from apm_cli.core.scope import get_apm_home

    key = _digest(str(Path(os.path.abspath(workspace))).encode("utf-8"))[:32]
    return get_apm_home() / "install-snapshots" / f"{_PREFIX}{key}.json"


def _write(path: Path, record: dict[str, Any]) -> None:
    from apm_cli.install.journal_modes import private_dir
    from apm_cli.utils.atomic_io import atomic_write_text

    private_dir(path.parent)
    atomic_write_text(path, json.dumps(record, sort_keys=True), new_file_mode=0o600)


def _fix(packages: list[str]) -> str:
    return (
        "apm install " + " ".join(shlex.quote(p) for p in packages) if packages else "apm install"
    )


class ManifestJournal:
    """The pre-install ``apm.yml`` of one attempt, kept on disk until its lock commits."""

    def __init__(
        self,
        manifest_path: Path,
        workspace: Path,
        prior: bytes | None,
        packages: list[str],
    ) -> None:
        from apm_cli.install.transaction import _committed_lockfile

        self.manifest_path = Path(os.path.abspath(manifest_path))
        self.workspace = Path(os.path.abspath(workspace))
        self.path = _record_path(self.workspace)
        self._record: dict[str, Any] = {
            "format": _FORMAT,
            "version": _VERSION,
            "workspace": str(self.workspace),
            "manifest": str(self.manifest_path),
            "prior": base64.b64encode(prior).decode("ascii") if prior is not None else None,
            "lock": _committed_lockfile(self.workspace),
            "written": [],
            "commit": None,
            "packages": list(packages),
        }
        _write(self.path, self._record)

    def seal(self) -> None:
        """Record the manifest this attempt wrote, so replay may undo exactly it."""
        try:
            digest = _digest(self.manifest_path.read_bytes())
        except OSError:
            return
        if digest not in self._record["written"]:
            self._record["written"].append(digest)
            _write(self.path, self._record)

    def mark_commit(self, lockfile_identity: str) -> None:
        """Name the lockfile about to commit: a kill after its write settles the record."""
        self.seal()
        self._record["commit"] = lockfile_identity
        _write(self.path, self._record)

    def discard(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def _load(path: Path, workspace: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("the record is a symbolic link")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or (record.get("format"), record.get("version")) != (
        _FORMAT,
        _VERSION,
    ):
        raise ValueError("the record has an unknown format or version")
    if record.get("workspace") != str(workspace):
        raise ValueError("the record names another workspace")
    written, packages, manifest = (
        record.get("written"),
        record.get("packages"),
        record.get("manifest"),
    )
    if not isinstance(manifest, str) or Path(manifest).parent != workspace:
        raise ValueError("the record names a manifest outside its workspace")
    if not isinstance(written, list):
        raise ValueError("the record is malformed")
    if not isinstance(packages, list) or not all(isinstance(p, str) for p in packages):
        raise ValueError("the record is malformed")
    prior = record.get("prior")
    record["prior"] = base64.b64decode(prior, validate=True) if prior is not None else None
    return record


def replay_abandoned_manifest(workspace: Path, logger: Any) -> None:
    """Restore the ``apm.yml`` an install killed before its lock commit left (#5454).

    Call under the lifecycle lock, before the command reads the manifest.
    Raises ``InstallFailureAlreadyRendered`` when the record cannot be read.
    """
    from apm_cli.install.errors import InstallFailureAlreadyRendered
    from apm_cli.install.transaction import _committed_lockfile, _restore_manifest_from_snapshot

    workspace = Path(os.path.abspath(workspace))
    path = _record_path(workspace)
    if not os.path.lexists(path):
        return
    try:
        record = _load(path, workspace)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        if logger is not None:
            logger.error(
                f"An interrupted install left a manifest record APM cannot read ({exc}): {path}. "
                f"Check that apm.yml declares what you want, delete {path}, "
                "then run the command again."
            )
        raise InstallFailureAlreadyRendered(f"unreadable manifest record {path}") from None
    lock = _committed_lockfile(workspace)
    if (record.get("commit") is not None and lock == record["commit"]) or lock != record.get(
        "lock"
    ):
        # The killed install's lockfile reached disk, or a later command changed it.
        path.unlink()
        return
    manifest, prior = Path(record["manifest"]), record["prior"]
    current = manifest.read_bytes() if manifest.is_file() else None
    fix = _fix(record["packages"])
    if current == prior:
        path.unlink()
        return
    if current is None or _digest(current) not in record["written"]:
        path.unlink()
        if logger is not None:
            logger.warning(
                f"An interrupted '{fix}' never locked what it added to {manifest.name}, which "
                "changed since, so it was kept as is. Run 'apm install' to lock what it declares."
            )
        return
    if prior is None:
        manifest.unlink()
    else:
        _restore_manifest_from_snapshot(manifest, prior)
    path.unlink()
    if logger is not None:
        logger.warning(
            f"An interrupted '{fix}' never locked what it added to {manifest.name}; "
            f"{manifest.name} was restored to its state before that install. "
            f"Run '{fix}' to add it again."
        )
