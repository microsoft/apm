"""Isolated prune casing fixture shared by command and CLI tests."""

from apm_cli.deps.lockfile import LockedDependency, LockFile


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
