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
process. Every writer of that ``apm.yml`` names the bytes it is about to
publish first (:func:`expect_manifest_text`), so a kill between the write and
its seal still replays, and publishes them atomically, so a kill never
leaves a partial apm.yml (:func:`publish_journaled_manifest`). The next install entry, under the lifecycle lock and before it reads
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
_NAMES = ("apm.yml", "apm.yaml")


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


def _remove(path: Path) -> None:
    """Delete the record and, like a discarded deploy journal, its parent once empty."""
    from apm_cli.utils.realpath_win import release_held

    global _ACTIVE
    if _ACTIVE is not None and _ACTIVE.path == path:
        _ACTIVE = None  # this process journals that record no longer

    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    with contextlib.suppress(OSError):
        release_held(path.parent)
        path.parent.rmdir()


def _lock_identity(workspace: Path) -> str | None:
    """The identity of the lockfile on disk, read as install reads it (legacy ``apm.lock`` too)."""
    from apm_cli.deps.lockfile import resolve_lockfile_path_for_read
    from apm_cli.install.deploy_snapshot import lockfile_identity

    try:
        path = resolve_lockfile_path_for_read(workspace, read_only=True)
        return lockfile_identity(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _quoted(words: list[str]) -> str:
    from apm_cli.utils.diagnostics import printable_ascii_text

    # The record is data on disk: never echo control or ANSI bytes to the terminal.
    return " ".join(shlex.quote(printable_ascii_text(w)) for w in words)


def retry_options(
    *, user_scope: bool, dev: bool, skills: Any = (), target: Any = None, root: Any = None
) -> tuple[list[str], list[str]]:
    """The ``apm install`` options that repeat an add, and those only a bootstrap needs.

    The scope options (``--global``, ``--root DIR``) come first: the kept-manifest
    advice repeats only those.
    """
    options = _scope_options(user_scope=user_scope, root=root) + (["--dev"] if dev else [])
    for skill in skills or ():
        options += ["--skill", str(skill)]
    if isinstance(target, list | tuple):
        target = ",".join(str(t) for t in target)
    return options, (["--target", str(target)] if target else [])


def _scope_options(*, user_scope: bool, root: Any) -> list[str]:
    return (["--global"] if user_scope else []) + (["--root", str(root)] if root else [])


def _scope_prefix(options: list[str]) -> list[str]:
    """The leading scope options (``--global``, ``--root DIR``) of a recorded option list."""
    prefix, rest = [], list(options)
    while rest and rest[0] in ("--global", "--root"):
        width = 1 if rest[0] == "--global" else 2
        prefix, rest = prefix + rest[:width], rest[width:]
    return prefix


_ACTIVE: ManifestJournal | None = None


def publish_journaled_manifest(path: Path, data: Any) -> bool:
    """Write round-trip YAML *data* to the journaled apm.yml *path*, atomically.

    Returns ``False`` when no install journals *path*: the caller writes it
    as before. Otherwise the bytes are named first, then published with
    ``os.replace``, so a kill never leaves a partial apm.yml (#5454).
    """
    journal = _ACTIVE
    if journal is None or Path(os.path.abspath(path)) != journal.manifest_path:
        return False
    from apm_cli.utils.yaml_io import yaml_roundtrip_to_str

    journal.publish(yaml_roundtrip_to_str(data))
    return True


class ManifestJournal:
    """The pre-install ``apm.yml`` of one attempt, kept on disk until its lock commits."""

    def __init__(
        self,
        manifest_path: Path,
        workspace: Path,
        prior: bytes | None,
        packages: list[str],
        options: tuple[list[str], list[str]] = ([], []),
    ) -> None:
        global _ACTIVE

        self.manifest_path = Path(os.path.abspath(manifest_path))
        self.workspace = Path(os.path.abspath(workspace))
        self.path = _record_path(self.workspace)
        self._record: dict[str, Any] = {
            "format": _FORMAT,
            "version": _VERSION,
            "workspace": str(self.workspace),
            "manifest": str(self.manifest_path),
            "prior": base64.b64encode(prior).decode("ascii") if prior is not None else None,
            "lock": _lock_identity(self.workspace),
            "written": [],
            "commit": None,
            "packages": list(packages),
            "options": list(options[0]),
            "bootstrap_options": list(options[1]),
        }
        _write(self.path, self._record)
        _ACTIVE = self

    def _add(self, digests: list[str]) -> None:
        new = [d for d in digests if d not in self._record["written"]]
        if new:
            self._record["written"].extend(new)
            _write(self.path, self._record)

    def expect(self, text: str) -> None:
        """Record the bytes a write is about to publish, as LF or platform newlines."""
        lf = text.replace("\r\n", "\n")
        self._add(
            [_digest(v.encode("utf-8")) for v in dict.fromkeys((lf, lf.replace("\n", os.linesep)))]
        )

    def publish(self, text: str) -> None:
        """Name *text*, then replace the manifest with it in one rename."""
        from apm_cli.utils.atomic_io import atomic_write_text, umask_file_mode, write_text_lf

        self.expect(text)
        if self.manifest_path.is_symlink():
            write_text_lf(self.manifest_path, text)  # a rename would replace the link
            return
        atomic_write_text(
            self.manifest_path, text, new_file_mode=umask_file_mode(self.manifest_path)
        )

    def seal(self) -> None:
        """Record the manifest this attempt wrote, so replay may undo exactly it."""
        try:
            self._add([_digest(self.manifest_path.read_bytes())])
        except OSError:
            return

    def mark_commit(self, lockfile_identity: str) -> None:
        """Name the lockfile about to commit: a kill after its write settles the record."""
        self.seal()
        self._record["commit"] = lockfile_identity
        _write(self.path, self._record)

    def discard(self) -> None:
        _remove(self.path)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


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
    manifest = record.get("manifest")
    if not isinstance(manifest, str) or Path(manifest).name not in _NAMES:
        raise ValueError("the record names a file other than an apm.yml")
    if not all(_strings(record.get(k)) for k in _LISTS):
        raise ValueError("the record is malformed")
    prior = record.get("prior")
    record["prior"] = base64.b64decode(prior, validate=True) if prior is not None else None
    return record


_LISTS = ("written", "packages", "options", "bootstrap_options")


def _retry(record: dict[str, Any]) -> str:
    """The exact ``apm install`` that repeats the interrupted add."""
    bootstrap = record["bootstrap_options"] if record["prior"] is None else []
    return _quoted([*record["options"], *bootstrap, *record["packages"]])


def _settle_other_source(record: dict[str, Any], target: Path, path: Path, logger: Any) -> None:
    """Refuse while another source's live record can still be replayed there; else drop it."""
    from apm_cli.core.remediation import INTERRUPTED_ADD_ORPHANED, INTERRUPTED_ADD_PENDING
    from apm_cli.install.errors import InstallFailureAlreadyRendered
    from apm_cli.utils.diagnostics import printable_ascii_text

    source = printable_ascii_text(str(target.parent))
    if not target.parent.is_dir():
        # Its source is gone: no command can replay the record there.
        _remove(path)
        if logger is not None:
            logger.warning(
                f"The interrupted 'apm install {_retry(record)}' from {source} can no longer "
                f"be restored: {source} is gone, so its record {path} was dropped. "
                f"{INTERRUPTED_ADD_ORPHANED.sentence()}"
            )
        return
    pending = INTERRUPTED_ADD_PENDING.bind(source=source)
    if scope := _scope_prefix(record["options"]):
        pending = pending.with_command(f"apm install {_quoted(scope)}")
    if logger is not None:
        logger.error(
            f"An interrupted 'apm install {_retry(record)}' from {source} is still pending "
            f"for this workspace. {pending.sentence()}"
        )
    raise InstallFailureAlreadyRendered(f"pending manifest record {path}")


def replay_abandoned_manifest(workspace: Path, logger: Any, manifest: Path) -> None:
    """Restore the ``apm.yml`` an install killed before its lock commit left (#5454).

    Call under the lifecycle lock, before the command reads *manifest*, the
    command's source ``apm.yml`` and the only file it may restore; a record
    naming any other file is left alone.
    Raises ``InstallFailureAlreadyRendered`` when the record cannot be read.
    """
    from apm_cli.core.remediation import INTERRUPTED_ADD_KEPT, INTERRUPTED_ADD_RETRY
    from apm_cli.install.errors import InstallFailureAlreadyRendered
    from apm_cli.install.transaction import _restore_manifest_from_snapshot

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
    target = Path(record["manifest"])
    lock = _lock_identity(workspace)
    if (record.get("commit") is not None and lock == record["commit"]) or lock != record.get(
        "lock"
    ):
        # The killed install's lockfile reached disk, or a later command changed
        # it: nothing is left to restore, whichever source the record names.
        _remove(path)
        return
    if target != Path(os.path.abspath(manifest)):
        # Never trust the record's own path, and never let another source's
        # install overwrite a live record: refuse until that source replays it.
        _settle_other_source(record, target, path, logger)
        return
    prior = record["prior"]
    current = target.read_bytes() if target.is_file() else None
    retry = _retry(record)
    fix = f"apm install {retry}"
    if current == prior:
        _remove(path)
        return
    if current is None or _digest(current) not in record["written"]:
        _remove(path)
        kept = INTERRUPTED_ADD_KEPT
        if scope := _scope_prefix(record["options"]):
            kept = kept.with_command(f"apm install {_quoted(scope)}")
        if logger is not None:
            logger.warning(
                f"An interrupted '{fix}' never locked what it added to {target.name}, which "
                f"changed since, so it was kept as is. {kept.sentence()}"
            )
        return
    if prior is None:
        target.unlink()
    else:
        _restore_manifest_from_snapshot(target, prior)
    _remove(path)
    if logger is not None:
        logger.warning(
            f"An interrupted '{fix}' never locked what it added to {target.name}; "
            f"{target.name} was restored to its state before that install. "
            f"{INTERRUPTED_ADD_RETRY.bind(packages=retry).sentence()}"
        )
