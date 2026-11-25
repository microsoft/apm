"""Startup and identity inputs of the verified-state record (#4977).

The CA bundle files TLS startup may load, the administrative lifecycle scripts,
the project root's identity and the update-notice schedule. Split out of
``install/verdict_record.py`` (install LOC budget); every input here only adds
misses.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from apm_cli.install.verdict_record import _tree
from apm_cli.utils.path_security import has_symlink_component, is_link_stat

# version_checker.should_check_for_updates: the daily check falls due past this age.
_NOTICE_PERIOD = 86400
_CLOCK_GRAIN = 2.0


def tls_surfaces(entry_env: dict[str, str] | None) -> list[str]:
    """The CA bundle files the run's entry environment selects, resolved.

    TLS startup reads their settings inside the recorded run (the env log keys
    them) and replay runs before it: the record keys each file's bytes as a
    surface, so a bundle that changed, went away or became unreadable misses and
    the CLI reports it.
    """
    from apm_cli.core.tls_trust import selectable_ca_files

    return [os.path.realpath(path) for path in selectable_ca_files(entry_env)]


def key_startup(digest: Any, root: Path, notice: str, surfaces: list[str]) -> None:
    """The administrative lifecycle scripts, the root's identity, the notice schedule,
    the apm_modules structure and the directories above every keyed surface."""
    _key_modules(digest, root)
    _key_parents(digest, root, surfaces)
    from apm_cli.core.lifecycle_scripts import _get_policy_scripts_dir

    for rel, files, dirs in _tree(str(_get_policy_scripts_dir())):
        # An admin script added after the record misses; user and project scripts
        # live in apm.yml files keyed above.
        listing = [entry.name for entry in files] + ["/", *dirs]
        digest.put(
            f"lifecycle-listing:{rel}", "\x00".join(listing).encode("utf-8", "surrogateescape")
        )
        for entry in files:
            digest.file(
                f"lifecycle:{rel}/{entry.name}" if rel else f"lifecycle:{entry.name}", entry.path
            )
    # The deployment receipts bind the root's device and inode (and birth time where
    # stat reports one): a recreated checkout misses even with byte-identical files.
    info = os.stat(root)
    birth = getattr(info, "st_birthtime", None)
    digest.put("root-identity", f"{info.st_dev}:{info.st_ino}:{birth!r}".encode())
    from apm_cli.update_policy import is_self_update_enabled

    digest.put("self-update", b"%d" % is_self_update_enabled())
    digest.put("notice", notice.encode("utf-8", "surrogateescape"))
    if notice:
        digest.put("notice-mtime", b"%d" % os.stat(notice).st_mtime_ns)


def notice_schedule(
    entry_time: float, *, output: bool = True, notified: bool = False
) -> tuple[str, float | None]:
    """The update-check file and when its daily check falls due, if the schedule
    decides the notice; ("", None) when another gate does.

    Raises OSError or ValueError (no record) when the check is due, or when this run
    ran it and printed the notice (*notified*), which the next ordinary run would
    not print. A run that ran the check and printed nothing records: the next run,
    with the check fresh, prints nothing either, and the record expires when it falls due.
    """
    from apm_cli import update_notice

    if not update_notice.schedule_decides():
        return "", None
    from apm_cli.utils.version_checker import get_update_cache_path

    path = get_update_cache_path()
    checked = os.stat(path).st_mtime
    # File times can trail the clock (a coarse kernel clock, 2 s on FAT).
    if output and notified and checked >= entry_time - _CLOCK_GRAIN:
        # Only a record of output replays it; an attested record replays none.
        raise ValueError("this run printed the update notice")
    return str(path), checked + _NOTICE_PERIOD


def hash_blind(package: Path) -> bool:
    """Whether a package copy holds entries its content hash skips but the source scan
    reads or refuses: links, non-regular files, and any entry named like an excluded dir."""
    from apm_cli.utils.content_hash import _EXCLUDED_DIRS

    for _rel, files, dirs in _tree(str(package)):
        if _EXCLUDED_DIRS.intersection(dirs) or any(
            entry.name in _EXCLUDED_DIRS or not entry.is_file(follow_symlinks=False)
            for entry in files
        ):
            return True
    return False


_MARK = {b"dir": "/", b"link": "@", b"file": "", b"absent": "?"}


def _kind(path: str) -> bytes:
    """One lstat: dir, link (a symlink or a junction, per path_security), file or absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return b"absent"
    except NotADirectoryError:
        return b"absent"
    if is_link_stat(info):
        return b"link"
    return b"dir" if stat.S_ISDIR(info.st_mode) else b"file"


def walk_governed(base: str) -> Iterator[tuple[str, list[os.DirEntry[str]], list[str]]]:
    """``_tree`` over one governed target root, refusing any linked directory level.

    The engine never deploys through a link (diff_apply._within, #5438): a root or
    a subdirectory that is a link or a junction is no verdict a hit can prove, so
    it raises (no record, a miss). ``_tree`` lists a link to a directory (or a
    dangling one) among the files, so a link that is not a link to a regular file
    is refused too. One lstat per directory level.
    """
    if _kind(base) == b"link":
        raise ValueError("linked target directory")
    for rel, files, dirs in _tree(base):
        for name in dirs:
            if _kind(os.path.join(base, rel, name)) == b"link":
                raise ValueError("linked target directory")
        for entry in files:
            if entry.is_symlink() and not entry.is_file():
                raise ValueError("linked target directory")
        yield rel, files, dirs


def has_linked_parent(root: Path, rel: str, proven: set[str]) -> bool:
    """Whether a directory between *root* and the file *rel* is a link or a junction,
    decided by the engine's owner (path_security.has_symlink_component, as
    diff_apply._within does).

    *proven* holds the parent directories already seen to be link-free in this check.
    """
    parent = os.path.dirname(rel)
    if not parent or parent in proven:
        return False
    if has_symlink_component(root, root / parent):
        return True
    proven.add(parent)
    return False


def _key_parents(digest: Any, root: Path, surfaces: list[str]) -> None:
    """The kind of each directory above a keyed surface file, below HOME or the root:
    a client's directory appearing can change what the no-op configures."""
    from apm_cli.install.verdict_record import _PROJECT_SURFACES, _USER_SURFACES

    home = os.path.expanduser("~")
    files = [*(os.path.join(root, rel) for rel in _PROJECT_SURFACES)]
    files += [os.path.join(home, rel) for rel in _USER_SURFACES]
    files += [path for path in surfaces if not path.startswith("\x00")]
    stops, seen = {str(root), home}, set()
    for path in sorted(files):
        parent = os.path.dirname(path)
        while parent not in stops and parent not in seen and os.path.dirname(parent) != parent:
            seen.add(parent)
            digest.put(f"parent:{parent}", _kind(parent))
            parent = os.path.dirname(parent)


def _key_modules(digest: Any, root: Path) -> None:
    """apm_modules down to the package copies (each one proven against the lock): an
    orphan directory or a leftover fast-apply marker changes the engine's verdict."""
    from apm_cli.install.cache_pin import MARKER_FILENAME

    stack = [("apm_modules", os.path.join(root, "apm_modules"))]
    digest.put("modules", _kind(stack[0][1]))
    while stack:
        rel, path = stack.pop()
        try:
            with os.scandir(path) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        names = [entry.name + _MARK[_kind(entry.path)] for entry in entries]
        digest.put(f"modules:{rel}", "\x00".join(names).encode("utf-8", "surrogateescape"))
        if MARKER_FILENAME in names:
            continue  # a package copy: its bytes are proven against the lock
        stack += [(f"{rel}/{e.name}", e.path) for e in entries if e.is_dir(follow_symlinks=False)]


def entry_stamp() -> list[Any]:
    """The recorded run's start: its wall time, the org-policy fetch attempts this
    process had made, and the policy cache's shape, so the writer knows whether the
    run's discovery fetched and whether the cache it read is still the one keyed."""
    import time

    from apm_cli.core.policy_fetch_ledger import fetch_attempts

    return [time.time(), fetch_attempts(), policy_cache_shape()]


def policy_cache_shape() -> str | None:
    """Every policy cache file's path, mtime and content digest (the cache discovery
    reads), read no-follow; None when any of it cannot be read (no record)."""
    import hashlib

    from apm_cli.install.verdict_record import _cache_root, _read_nofollow

    try:
        cache = _cache_root()
        if cache is None:
            return ""
        rows = []
        for rel, files, _dirs in _tree(str(cache / "policy_v1")):
            for entry in files:
                data = _read_nofollow(entry.path)
                digest = "absent" if data is None else hashlib.sha256(data).hexdigest()
                mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                rows.append(f"{rel}/{entry.name}:{mtime}:{digest}")
        return "\n".join(rows)
    except (OSError, ValueError):
        return None
