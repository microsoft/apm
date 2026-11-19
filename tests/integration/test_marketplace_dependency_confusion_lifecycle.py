"""Real-CLI #1326 dependency-confusion refusal on every marketplace install path (#3913).

An enterprise (``*.ghe.com``) marketplace, registered over SSH and served by a
local Git fixture, lists plugins whose dict sources (``type: github``,
``type: git-subdir`` and ``type: url``, #3937) have a bare cross-repo
``acme/tools`` locator. The bare form falls back to public ``github.com``,
where the test stages a repository under that exact name. Every path that
turns the entry into a dependency -- ``apm install NAME@MARKETPLACE``, an
``apm.yml`` entry (with or without a version range), a transitive declaration
and ``apm update`` -- must refuse it before any write or Git request to that
repository (a ``GIT_TRACE`` log is the tripwire). A host-qualified
``repo: github.com/acme/tools`` entry and an in-marketplace ``git-subdir``
entry still install, as do in-marketplace ``repository:``/``url:`` synonyms
and a bare entry when ``GITHUB_HOST`` is the marketplace host (#3945).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_MARKETPLACE = "corp-mkt"
_CATALOG_REMOTE = "ssh://git@corp.ghe.com/corp/marketplace"
_CATALOG_HTTPS = "https://corp.ghe.com/corp/marketplace"
_BARE = "shared-tool"
_QUALIFIED = "public-tool"
_SUBDIR = "plugins/x"
# Cross-repo plugin names by dict source type; bare and host-qualified pairs.
_BARE_BY_TYPE = {"github": _BARE, "git-subdir": "shared-subdir-tool", "url": "shared-url-tool"}
_QUALIFIED_BY_TYPE = {"github": _QUALIFIED, "git-subdir": "public-subdir-tool"}
_IN_MARKETPLACE = "internal-subdir-tool"
_IN_MARKETPLACE_SYNONYMS = ("internal-repository-tool", "internal-url-subdir-tool")
_ATTACKER_REMOTE = "https://github.com/acme/tools"
_ENTERPRISE_REMOTE = "https://corp.ghe.com/acme/tools"
_TARGET = "copilot"
_FLAGS = ("--target", _TARGET, "--parallel-downloads", "0")
_REFUSAL = "refused (dependency-confusion risk #1326)"


@dataclass
class _World:
    root: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner
    repositories: LocalGitRepositoryFactory
    catalog: LocalGitRepository
    package: LocalGitRepository
    trace: Path

    def publish(self, bare_repo: str) -> None:
        subdir = {"type": "git-subdir", "subdir": _SUBDIR}
        plugins = [
            {"name": _BARE, "source": {"type": "github", "repo": bare_repo}},
            {"name": _QUALIFIED, "source": {"type": "github", "repo": "github.com/acme/tools"}},
            {"name": _BARE_BY_TYPE["git-subdir"], "source": {**subdir, "repo": "acme/tools"}},
            {"name": _BARE_BY_TYPE["url"], "source": {"type": "url", "url": "acme/tools"}},
            {
                "name": _QUALIFIED_BY_TYPE["git-subdir"],
                "source": {**subdir, "repo": "github.com/acme/tools"},
            },
            {
                "name": _IN_MARKETPLACE,
                "source": {"type": "git-subdir", "repo": "corp/marketplace", "subdir": "plugins/y"},
            },
            {
                "name": _IN_MARKETPLACE_SYNONYMS[0],
                "source": {"type": "github", "repository": "corp/marketplace", "path": "plugins/y"},
            },
            {
                "name": _IN_MARKETPLACE_SYNONYMS[1],
                "source": {"type": "git-subdir", "url": "corp/marketplace", "subdir": "plugins/y"},
            },
        ]
        (self.catalog.worktree / "marketplace.json").write_text(
            json.dumps({"name": _MARKETPLACE, "owner": {"name": "Corp"}, "plugins": plugins}),
            encoding="utf-8",
        )
        self.repositories.commit(self.catalog, message=f"publish {bare_repo}")

    def write_manifest(self, path: Path, dependencies: list[object]) -> None:
        path.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": path.name,
            "version": "1.0.0",
            "targets": [_TARGET],
            "dependencies": {"apm": dependencies},
        }
        dump_yaml(manifest, path / "apm.yml")

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(
            args,
            scenario_id="marketplace-dependency-confusion",
            cwd=cwd or self.root,
            env=self.env,
        )

    def contacted_package(self) -> bool:
        """True when any Git process logged in ``trace`` named the staged repository."""
        if not self.trace.exists():
            return False
        log = self.trace.read_text(encoding="utf-8", errors="replace")
        return _ATTACKER_REMOTE in log or str(self.package.origin.resolve()) in log


def _with_ssh_rewrite(env: dict[str, str], repository: LocalGitRepository) -> dict[str, str]:
    """Route the SSH catalog remote to the local fixture (the harness rewrites HTTPS only)."""
    count = int(env["GIT_CONFIG_COUNT"])
    for offset, remote in enumerate((_CATALOG_REMOTE, f"{_CATALOG_REMOTE}.git")):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = f"url.{repository.file_url}/.insteadOf"
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = remote
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    return env


def _write_skill(root: Path, name: str) -> None:
    skill = root / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: staged\n---\n# {name}\n", encoding="utf-8")


def _world(tmp_path: Path, binary: Path, *, bare_repo: str = "acme/tools") -> _World:
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    package = repositories.create("acme-tools")
    _write_skill(package.worktree, "pwned")
    _write_skill(package.worktree / _SUBDIR, "pwned")
    repositories.tag(package, "v1.0.0", repositories.commit(package, message="stage"))
    catalog = repositories.create("corp-marketplace")
    _write_skill(catalog.worktree / "plugins" / "y", "internal")
    enterprise = repositories.create("corp-acme-tools")
    _write_skill(enterprise.worktree, "trusted")
    _write_skill(enterprise.worktree / _SUBDIR, "trusted")
    repositories.commit(enterprise, message="enterprise package")
    env = repositories.url_rewrite_subprocess_env_many(
        (
            (package, _ATTACKER_REMOTE),
            (catalog, _CATALOG_HTTPS),
            (enterprise, _ENTERPRISE_REMOTE),
        )
    )
    trace = isolated.work_root / "git-trace.log"
    env["GIT_TRACE"] = str(trace)
    world = _World(
        root=isolated.work_root / "consumer",
        env=_with_ssh_rewrite(env, catalog),
        runner=ApmLifecycleRunner((str(binary),), timeout_seconds=120),
        repositories=repositories,
        catalog=catalog,
        package=package,
        trace=trace,
    )
    world.publish(bare_repo)
    world.root.mkdir(parents=True)
    added = world.run("marketplace", "add", f"{_CATALOG_REMOTE}.git", "--name", _MARKETPLACE)
    assert added.returncode == 0, added.stdout + added.stderr
    return world


def _arrange(world: _World, path: str, plugin: str = _BARE) -> tuple[str, ...]:
    """Lay out the consumer for *path* and return the command that resolves *plugin*."""
    entry: dict[str, object] = {"name": plugin, "marketplace": _MARKETPLACE}
    if path == "cli":
        world.write_manifest(world.root, [])
        return ("install", f"{plugin}@{_MARKETPLACE}", *_FLAGS)
    if path == "transitive":
        world.write_manifest(world.root.parent / "middle", [entry])
        world.write_manifest(world.root, ["../middle"])
        return ("install", *_FLAGS)
    if path == "apm-yml-range":
        # The staged repo has no matching tag: a tag lookup before admission
        # would fail with a version error instead of the #1326 refusal.
        entry["version"] = "^1.0.0"
    world.write_manifest(world.root, [entry])
    if path == "update":
        return ("update", "--yes", *_FLAGS)
    return ("install", *_FLAGS)


_PATHS = ("cli", "apm-yml", "apm-yml-range", "transitive", "update")


@pytest.mark.parametrize(
    ("path", "source_type"),
    [
        *((path, source_type) for path in _PATHS for source_type in ("git-subdir", "github")),
        # ``type: url`` reaches the sentinel through a structured reference; the
        # admission owner is shared, so one path proves it.
        ("cli", "url"),
    ],
)
def test_bare_cross_repo_marketplace_entry_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path, path: str, source_type: str
) -> None:
    """The #1326 refusal fires on every path; nothing is fetched, written or deployed."""
    world = _world(tmp_path, apm_binary_path)
    command = _arrange(world, path, _BARE_BY_TYPE[source_type])
    manifest_before = (world.root / "apm.yml").read_bytes()
    world.trace.unlink(missing_ok=True)

    result = world.run(*command)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode != 0, output
    assert _REFUSAL in output, output
    assert "'corp.ghe.com/acme/tools'" in output
    assert "'github.com/acme/tools'" in output
    assert (world.root / "apm.yml").read_bytes() == manifest_before
    assert not (world.root / "apm.lock.yaml").exists()
    assert not (world.root / "apm_modules" / "acme").exists()
    assert not list(world.root.rglob("pwned"))
    assert not world.contacted_package(), "a Git request reached the github.com lookalike"


@pytest.mark.parametrize(
    ("plugin", "skill"),
    [
        (_QUALIFIED_BY_TYPE["github"], "pwned"),
        (_QUALIFIED_BY_TYPE["git-subdir"], "pwned"),
        (_IN_MARKETPLACE, "internal"),
        (_IN_MARKETPLACE_SYNONYMS[0], "internal"),
        (_IN_MARKETPLACE_SYNONYMS[1], "internal"),
    ],
)
def test_host_qualified_or_in_marketplace_entry_still_installs(
    tmp_path: Path, apm_binary_path: Path, plugin: str, skill: str
) -> None:
    """The documented escape hatch and in-marketplace sources are not refused."""
    world = _world(tmp_path, apm_binary_path)
    world.write_manifest(world.root, [{"name": plugin, "marketplace": _MARKETPLACE}])

    result = world.run("install", *_FLAGS)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode == 0, output
    assert _REFUSAL not in output
    assert (world.root / "apm.lock.yaml").exists()
    assert list(world.root.rglob(f"{skill}/SKILL.md"))
    # Positive control for the refusal test's tripwire: a real fetch is logged.
    assert world.contacted_package() is (skill == "pwned")
    if skill == "internal":
        # In-marketplace entries resolve to the enterprise host (#1285, #3945).
        assert "corp.ghe.com" in (world.root / "apm.lock.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("source_type", ["git-subdir", "github"])
def test_bare_entry_is_admitted_when_github_host_is_the_marketplace_host(
    tmp_path: Path, apm_binary_path: Path, source_type: str
) -> None:
    """With ``GITHUB_HOST`` set to the marketplace host, bare means that host (#3945)."""
    world = _world(tmp_path, apm_binary_path)
    world.env["GITHUB_HOST"] = "corp.ghe.com"
    entry = {"name": _BARE_BY_TYPE[source_type], "marketplace": _MARKETPLACE}
    world.write_manifest(world.root, [entry])
    world.trace.unlink(missing_ok=True)

    result = world.run("install", *_FLAGS)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode == 0, output
    assert _REFUSAL not in output
    assert list(world.root.rglob("trusted/SKILL.md"))
    assert not list(world.root.rglob("pwned"))
    assert not world.contacted_package(), "a Git request reached the github.com lookalike"


def test_bare_entry_is_still_refused_when_github_host_is_another_host(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``GITHUB_HOST`` naming a different host does not admit the bare entry (#3945)."""
    world = _world(tmp_path, apm_binary_path)
    world.env["GITHUB_HOST"] = "other.ghe.com"
    command = _arrange(world, "cli", _BARE_BY_TYPE["git-subdir"])
    world.trace.unlink(missing_ok=True)

    result = world.run(*command)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode != 0, output
    assert _REFUSAL in output, output
    assert not list(world.root.rglob("pwned"))
    assert not world.contacted_package()


def test_apm_update_re_admits_an_entry_locked_before_its_catalog_turned_ambiguous(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A locked entry whose catalog row is now bare is refused on ``apm update``.

    Plain installs replay the lock without the catalog (#3911); ``apm update``
    re-resolves through admission, so it is the remediation for lock rows an
    older APM wrote.
    """
    world = _world(tmp_path, apm_binary_path, bare_repo="github.com/acme/tools")
    world.write_manifest(world.root, [{"name": _BARE, "marketplace": _MARKETPLACE}])
    installed = world.run("install", *_FLAGS)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    lock_before = (world.root / "apm.lock.yaml").read_bytes()
    world.publish("acme/tools")

    result = world.run("update", "--yes", *_FLAGS)

    output = " ".join(f"{result.stdout}\n{result.stderr}".split())
    assert result.returncode != 0, output
    assert _REFUSAL in output, output
    assert (world.root / "apm.lock.yaml").read_bytes() == lock_before
