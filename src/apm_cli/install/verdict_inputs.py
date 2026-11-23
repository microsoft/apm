"""Startup and identity inputs of the verified-state record (#4977).

The CA bundle files TLS startup may load, the administrative lifecycle scripts,
the project root's identity and the update-notice schedule. Split out of
``install/verdict_record.py`` (install LOC budget); every input here only adds
misses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from apm_cli.install.verdict_record import _tree

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


def key_startup(digest: Any, root: Path, notice: str) -> None:
    """The administrative lifecycle scripts, the root's identity and the notice schedule."""
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


def notice_schedule(entry_time: float) -> tuple[str, float | None]:
    """The update-check file and when its daily check falls due, if the schedule
    decides the notice; ("", None) when another gate does.

    Raises OSError or ValueError (no record) when the check is due or this run ran
    it: that run may have printed a notice the next ordinary run would not.
    """
    from apm_cli import update_notice

    if not update_notice.schedule_decides():
        return "", None
    from apm_cli.utils.version_checker import get_update_cache_path

    path = get_update_cache_path()
    checked = os.stat(path).st_mtime
    # File times can trail the clock (a coarse kernel clock, 2 s on FAT).
    if checked >= entry_time - _CLOCK_GRAIN:
        raise ValueError("this run checked for updates")
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
