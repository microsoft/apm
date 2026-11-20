"""Copy-on-write clone of the lifecycle template world (#4094).

A clone copies the template root (packages, project, HOME, APM_HOME, cache,
bundle) with a reflink copy where the filesystem supports it
(``cp --reflink=auto`` on Linux, ``cp -c`` clonefile on macOS), else
``shutil.copytree``. Every absolute path the world carries (env values,
package specs) moves to the clone; the shared services and their git
repositories stay with the template.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
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


def remove_tree(path: Path, *, missing_ok: bool = True) -> None:
    """Remove *path*; git writes read-only pack files, which Windows will not delete."""
    if not path.exists():
        if missing_ok:
            return
        raise FileNotFoundError(path)
    for root, _dirs, files in os.walk(path):
        for name in files:
            os.chmod(os.path.join(root, name), stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(path)


def copy_tree(source: Path, dest: Path) -> str:
    """Copy *source* to a new *dest*; return the method used (reflink or copytree)."""
    command = _reflink_command(source, dest)
    if command is not None:
        result = subprocess.run(command, capture_output=True, check=False, timeout=60)
        if result.returncode == 0:
            return "reflink"
        remove_tree(dest)
    shutil.copytree(source, dest, symlinks=True)
    return "copytree"


def clone(template: World, dest: Path) -> World:
    """Copy *template* to *dest* (which must not exist), rewriting every path it carries."""
    dest = dest.parent.resolve() / dest.name
    if dest.exists():
        raise FileExistsError(dest)
    copy_tree(template.root, dest)
    # Only whole path components move: "<root>-shared" keeps pointing at the template.
    pattern = re.compile(re.escape(str(template.root)) + r"(?=$|[/\\:;])")

    def rewrite(value: str) -> str:
        return pattern.sub(lambda _match: str(dest), value)

    def moved(path: Path) -> Path:
        return Path(rewrite(str(path)))

    return replace(
        template,
        root=dest,
        project=moved(template.project),
        home=moved(template.home),
        apm_home=moved(template.apm_home),
        env={key: rewrite(value) for key, value in template.env.items()},
        packages={
            key: PackageRef(ref.name, rewrite(ref.spec), ref.kinds, ref.source)
            for key, ref in template.packages.items()
        },
        cache_root=moved(template.cache_root) if template.cache_root else None,
        user_cwd=moved(template.user_cwd) if template.user_cwd else None,
    )
