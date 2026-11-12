"""Real-binary contract: package links install as content on every host (#3457).

The dependency commits contained file and directory links as mode-120000
entries, so the fixture needs no OS symlink capability. ``apm install``
must record the same ``content_hash`` on Linux, macOS and Windows (pinned
below), ``apm install --frozen`` must replay that lock from a cold cache, and
an escaping link must fail before anything is hashed or locked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_SOURCE = "https://github.com/acme/linked-pkg.git"
_INSTALL = ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0")
_SKILL = b"---\nname: linked\ndescription: Skill whose reference is a package link\n---\n# Linked\n"
_REFERENCE = b"Shared reference used through a link\n"
_PACKAGE_FILES = {
    "apm.yml": b"name: linked-pkg\nversion: 1.0.0\ndescription: Package with links\n",
    ".apm/skills/linked/SKILL.md": _SKILL,
}
_SHARED_FILES = {
    "shared/reference.md": _REFERENCE,
    "shared/notes/usage.md": b"Usage notes\n",
}
# Package-relative links; ``{root}`` climbs from the package to the
# repository root, where ``shared/`` lives.
_LINKS = {
    ".apm/skills/linked/reference.md": "../../../{root}shared/reference.md",
    ".apm/skills/linked/assets": "../../../{root}shared/notes",
    "AGENTS.md": "{root}shared/reference.md",
}
# Hash of the tree a POSIX install records when it follows these links; every
# platform must reproduce it so a lockfile from any host replays anywhere.
# ``subdirectory`` installs ``packages/linked`` of a monorepo whose links
# leave the package path (and its sparse cone) for the repository root.
_LAYOUTS = {
    "whole-repo": (
        "",
        "sha256:5dc1b14f6eee2410d3f84e28ebcea1f0ed163c4d7b00e4849403612c8862d63f",
    ),
    "subdirectory": (
        "packages/linked",
        "sha256:53a8b0e13ce1bed0f3aca44c710a35ce9935fd48c6fe2726ec96a56e4b4d3a4c",
    ),
}


def _git(env: dict[str, str], *args: str, input: str | None = None) -> str:
    return subprocess.run(
        ("git", *args),
        env=env,
        input=input,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _publish(
    isolated: IsolatedApmEnvironment, env: dict[str, str], links: dict[str, str], prefix: str
) -> str:
    """Commit the package with links as index entries; rewrite the URL to it."""
    work = isolated.repository_root / "linked-pkg-worktree"
    bare = isolated.repository_root / "linked-pkg.git"
    _git(env, "init", "-q", "--initial-branch=main", str(work))
    lead = f"{prefix}/" if prefix else ""
    files = {**{f"{lead}{path}": data for path, data in _PACKAGE_FILES.items()}, **_SHARED_FILES}
    for relative, content in files.items():
        path = work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(env, "-C", str(work), "add", *files)
    climb = "../" * len(prefix.split("/")) if prefix else ""
    for name, link_target in links.items():
        relative = f"{lead}{name}"
        target = link_target.format(root=climb)
        oid = _git(env, "-C", str(work), "hash-object", "-w", "--stdin", input=target)
        _git(
            env, "-C", str(work), "update-index", "--add", "--cacheinfo", f"120000,{oid},{relative}"
        )
    _git(env, "-C", str(work), "commit", "-q", "-m", "linked package")
    _git(env, "clone", "-q", "--bare", str(work), str(bare))
    _git(env, "config", "--global", "--add", f"url.{bare.resolve().as_uri()}.insteadOf", _SOURCE)
    return _git(env, "-C", str(work), "rev-parse", "HEAD")


@pytest.fixture
def short_root() -> Iterator[Path]:
    """Keep staging clones under Windows MAX_PATH; pytest basetemp nests deeply."""
    root = Path(tempfile.mkdtemp(prefix="apl"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _scenario(
    root: Path, links: dict[str, str], prefix: str = ""
) -> tuple[Path, dict[str, str], str]:
    isolated = IsolatedApmEnvironment.create(root / "e", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    sha = _publish(isolated, env, links, prefix)
    dependency = {"git": _SOURCE, "ref": sha, **({"path": prefix} if prefix else {})}
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "target": "copilot",
            "dependencies": {"apm": [dependency]},
        },
        project / "apm.yml",
    )
    return project, env, sha


def _run(apm_binary_path: Path, args: tuple[str, ...], project: Path, env: dict[str, str]):
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    return runner.run(args, cwd=project, env=env)


def _ok(result: CommandResult) -> None:
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"


def _locked(project: Path) -> dict[str, object]:
    (dependency,) = load_yaml(project / "apm.lock.yaml")["dependencies"]
    return dependency


def _regular_tree(root: Path) -> dict[str, bytes]:
    found = {}
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink(), path
        if path.is_file() and path.name != ".apm-pin":
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_links_install_as_content_and_frozen_replays_the_cross_platform_hash(
    short_root: Path, apm_binary_path: Path, layout: str
) -> None:
    prefix, golden = _LAYOUTS[layout]
    project, env, sha = _scenario(short_root, _LINKS, prefix)
    install_root = project / "apm_modules" / "acme" / "linked-pkg" / prefix

    _ok(_run(apm_binary_path, _INSTALL, project, env))

    locked = _locked(project)
    assert locked["resolved_commit"] == sha
    assert locked["content_hash"] == golden
    installed = _regular_tree(install_root)
    assert installed["AGENTS.md"] == _REFERENCE
    assert installed[".apm/skills/linked/reference.md"] == _REFERENCE
    assert installed[".apm/skills/linked/assets/usage.md"] == b"Usage notes\n"
    deployed = project / ".agents" / "skills" / "linked"
    assert (deployed / "reference.md").read_bytes() == _REFERENCE
    assert not (deployed / "reference.md").is_symlink()

    # Replay the lock, as recorded on any platform, from a cold cache.
    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    shutil.rmtree(project / "apm_modules")
    shutil.rmtree(Path(env["APM_CACHE_DIR"]))
    _ok(_run(apm_binary_path, (*_INSTALL, "--frozen"), project, env))

    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert _regular_tree(install_root) == installed


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("../../../{root}../outside.md", "resolves outside the repository"),
        ("../../../{root}missing.md", "is not a tracked file or directory"),
        ("..", "forms a cycle"),
    ],
    ids=["escaping", "broken", "cyclic"],
)
@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_unresolvable_link_fails_before_hashing(
    short_root: Path, apm_binary_path: Path, target: str, message: str, layout: str
) -> None:
    prefix, _ = _LAYOUTS[layout]
    links = {**_LINKS, ".apm/skills/linked/bad.md": target}
    project, env, _ = _scenario(short_root, links, prefix)

    result = _run(apm_binary_path, _INSTALL, project, env)

    assert result.returncode != 0
    output = " ".join(f"{result.stdout} {result.stderr}".split())
    assert ".apm/skills/linked/bad.md" in output
    assert message in output
    assert not (project / "apm.lock.yaml").exists()
