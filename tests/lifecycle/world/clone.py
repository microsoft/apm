"""Copy-on-write clone of the lifecycle template world (#4094).

A clone copies the template root (packages, project, HOME, APM_HOME, cache,
bundle) with a reflink copy where the filesystem supports it
(``cp --reflink=auto`` on Linux, ``cp -c`` clonefile on macOS), else
``shutil.copytree``. Every absolute path the world carries (env values,
package specs) moves to the clone; the shared services and their git
repositories stay with the template.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from tests.lifecycle.world.contract import PackageRef, World


def _reflink_command(source: Path, dest: Path) -> tuple[str, ...] | None:
    cp = shutil.which("cp")
    if cp is None:
        return None
    if sys.platform.startswith("linux"):
        return (cp, "-a", "--reflink=auto", str(source), str(dest))
    if sys.platform == "darwin":
        return (cp, "-c", "-R", "-p", str(source), str(dest))
    return None


def copy_tree(source: Path, dest: Path) -> str:
    """Copy *source* to a new *dest*; return the method used (reflink or copytree)."""
    command = _reflink_command(source, dest)
    if command is not None:
        result = subprocess.run(command, capture_output=True, check=False, timeout=60)
        if result.returncode == 0:
            return "reflink"
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(source, dest, symlinks=True)
    return "copytree"


def clone(template: World, dest: Path) -> World:
    """Copy *template* to *dest* (which must not exist), rewriting every path it carries."""
    dest = dest.parent.resolve() / dest.name
    if dest.exists():
        raise FileExistsError(dest)
    copy_tree(template.root, dest)
    old, new = str(template.root), str(dest)

    def moved(path: Path) -> Path:
        return Path(str(path).replace(old, new, 1))

    return replace(
        template,
        root=dest,
        project=moved(template.project),
        home=moved(template.home),
        apm_home=moved(template.apm_home),
        env={key: value.replace(old, new) for key, value in template.env.items()},
        packages={
            key: PackageRef(ref.name, ref.spec.replace(old, new, 1), ref.kinds, ref.source)
            for key, ref in template.packages.items()
        },
        cache_root=moved(template.cache_root) if template.cache_root else None,
        user_cwd=moved(template.user_cwd) if template.user_cwd else None,
    )
