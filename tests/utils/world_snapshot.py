"""Restore a lifecycle world in place, keeping the directories that survive.

Deployment receipts bind to a deployment root's inode, so a generated model
cannot replay from a fresh ``copytree`` of its world. ``restore_tree`` makes a
destination byte-equal to a pristine copy while every directory present on
both sides is synchronized in place rather than recreated.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def restore_tree(source: Path, destination: Path) -> None:
    """Make *destination* byte-equal to *source*, keeping surviving directories."""
    destination.mkdir(exist_ok=True)
    wanted = {entry.name: entry for entry in os.scandir(source)}
    for entry in os.scandir(destination):
        if entry.name not in wanted:
            _remove_path(Path(entry.path))
    for name, entry in wanted.items():
        target = destination / name
        if entry.is_dir(follow_symlinks=False):
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                _remove_path(target)
            restore_tree(Path(entry.path), target)
            continue
        if target.is_symlink() or target.exists():
            _remove_path(target)
        if entry.is_symlink():
            target.symlink_to(os.readlink(entry.path))
        else:
            shutil.copy2(entry.path, target)
    shutil.copystat(source, destination)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
