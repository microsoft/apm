"""Isolated prune casing fixture shared by command and CLI tests."""

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from apm_cli.deps.lockfile import LockedDependency, LockFile


class CaseSensitiveMaterializationReader:
    """Model exact directory spellings without claiming a native filesystem."""

    def __init__(self, root: Path, directories: Iterable[str]) -> None:
        self.root = root.absolute()
        self.children: dict[tuple[str, ...], set[str]] = {(): set()}
        for directory in directories:
            parts = PurePosixPath(directory).parts
            for index, name in enumerate(parts):
                self.children.setdefault(parts[:index], set()).add(name)
                self.children.setdefault(parts[: index + 1], set())

    def exists(self, path: Path) -> bool:
        return path.absolute().relative_to(self.root).parts in self.children

    def is_dir(self, path: Path) -> bool:
        return self.exists(path)

    def iterdir(self, path: Path) -> Iterable[Path]:
        parts = path.absolute().relative_to(self.root).parts
        return (path / name for name in sorted(self.children[parts]))

    def samefile(self, left: Path, right: Path) -> bool:
        return left.absolute() == right.absolute()

    def invalidate(self, *paths: Path) -> None:
        return None


def _snapshot(root: Path) -> dict[str, bytes]:
    """Capture file names and bytes, including user content."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _prefix_project(root: Path, *, duplicate: bool) -> tuple[Path, tuple[str, ...]]:
    """Create distinct repos or duplicate identities under case-variant owners."""
    second = "microsoft/apm" if duplicate else "microsoft/Other"
    declared = "Microsoft/APM" if duplicate else "Microsoft/APM\n    - microsoft/Other"
    _, orphan = _project(root, declared, "Microsoft/APM", with_lock=False)
    package = root / "apm_modules" / second
    package.mkdir(parents=True, exist_ok=True)
    (package / "apm.yml").write_text("name: second\nversion: 1.0.0\n", encoding="utf-8")
    (package / "notes.txt").write_bytes(b"second package user content\n")
    return orphan, ("Microsoft/APM", second, "other/orphan")


def _project(root, declared, installed, *, with_lock):
    (root / "apm.yml").write_text(
        "name: casing-fixture\nversion: 1.0.0\ntargets: [agent-skills]\n"
        f"dependencies:\n  apm:\n    - {declared}\n  mcp: []\n",
        encoding="utf-8",
    )
    package = root / "apm_modules" / installed
    package.mkdir(parents=True)
    (package / "apm.yml").write_text("name: retained\nversion: 1.0.0\n", encoding="utf-8")
    (package / "notes.txt").write_bytes(b"user content\n")
    orphan = root / "apm_modules" / "other" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "apm.yml").write_text("name: orphan\nversion: 1.0.0\n", encoding="utf-8")
    if with_lock:
        LockFile(
            dependencies={
                installed.lower(): LockedDependency(
                    repo_url=installed, resolved_commit="a" * 40, depth=1
                ),
                "other/orphan": LockedDependency(repo_url="other/orphan", depth=1),
            }
        ).write(root / "apm.lock.yaml")
    return package, orphan
