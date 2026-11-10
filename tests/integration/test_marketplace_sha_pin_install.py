"""Real-binary contracts for marketplace source ``sha`` pins (microsoft/apm#3426)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import GitCommit, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_MARKETPLACE_NAME = "sha-pin-marketplace"
_HOST = "git.example.invalid"
_GITHUB_REPO = "acme/sha-github-pkg"
_URL_REPO = "acme/sha-url-pkg"
_MONO_REPO = "acme/sha-monorepo"
_SUBDIR_PACKAGE = "sha-subdir-pkg"
_PACK_REPO = "acme/sha-pack-pkg"
_BAD_SHAS = {
    "short": "abc123",
    "non-hex": "z" * 40,
}


@dataclass(frozen=True)
class _Shape:
    """One remote source shape and the package skill it deploys."""

    id: str
    skill: str
    repo: str

    def source(self, commit: GitCommit | str | None, ref: str | None) -> dict[str, object]:
        """Build a catalog source object with optional ref and sha."""
        if self.id == "github":
            source: dict[str, object] = {"source": "github", "repo": self.repo}
        elif self.id == "url":
            source = {"source": "url", "url": f"https://{_HOST}/{self.repo}"}
        else:
            source = {
                "source": "git-subdir",
                "url": f"https://{_HOST}/{self.repo}",
                "path": f"packages/{self.skill}",
            }
        if ref is not None:
            source["ref"] = ref
        if commit is not None:
            source["sha"] = commit if isinstance(commit, str) else commit.sha
        return source


_SHAPES = (
    _Shape("github", "sha-github-pkg", _GITHUB_REPO),
    _Shape("url", "sha-url-pkg", _URL_REPO),
    _Shape("git-subdir", _SUBDIR_PACKAGE, _MONO_REPO),
)
_SHAPE_BY_ID = {shape.id: shape for shape in _SHAPES}


@dataclass(frozen=True)
class _Fixture:
    """Hermetic repositories, catalog, and command environment."""

    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    runner: ApmLifecycleRunner
    repositories: LocalGitRepositoryFactory
    catalog_root: Path
    old_commits: dict[str, GitCommit]
    new_commits: dict[str, GitCommit]


def _skill_document(name: str, body: str) -> str:
    """Return one valid skill whose body identifies the commit."""
    return f"---\nname: {name}\ndescription: Sha pin contract skill {name}\n---\n# {name}\n{body}\n"


def _configure_local_rewrite(
    remote_url: str,
    file_url: str,
    *,
    environment: dict[str, str],
) -> None:
    """Route one production-shaped HTTPS remote to a local bare repository."""
    for candidate in (f"{remote_url}.git", remote_url):
        for command in (
            ("git", "config", "--global", "--add", f"url.{file_url}.insteadOf", candidate),
            (
                "git",
                "config",
                "--file",
                str(Path(environment["HOME"]) / ".gitconfig"),
                "--add",
                f"url.{file_url}.insteadOf",
                candidate,
            ),
        ):
            subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )


def _command_evidence(result: CommandResult) -> str:
    """Render stable command evidence for assertions."""
    return (
        f"cwd={str(result.cwd)!r}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _seed_two_commits(
    repositories: LocalGitRepositoryFactory,
    name: str,
    skill_file: Path,
    skill: str,
    *,
    source_tree: Path,
    rewrite_url: str,
    environment: dict[str, str],
) -> tuple[GitCommit, GitCommit]:
    """Commit v1 then v2 of one skill and route its remote to the local repo."""
    repository = repositories.create(name, source_tree=source_tree)
    old = repositories.commit(repository, message="v1")
    skill_path = repository.worktree / skill_file
    skill_path.write_text(_skill_document(skill, "v2"), encoding="utf-8")
    new = repositories.commit(repository, message="v2")
    assert old.sha != new.sha
    _configure_local_rewrite(rewrite_url, repository.file_url, environment=environment)
    return old, new


def _create_fixture(root: Path, binary: Path) -> _Fixture:
    """Create one repo per source shape, each with an old and a newer commit."""
    isolated = IsolatedApmEnvironment.create(root / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    factory = LocalPackageFactory(isolated.package_root)
    old: dict[str, GitCommit] = {}
    new: dict[str, GitCommit] = {}

    for shape in (_SHAPE_BY_ID["github"], _SHAPE_BY_ID["url"]):
        package = factory.create(shape.skill, targets=("copilot",))
        factory.add_skill(package, shape.skill, _skill_document(shape.skill, "v1"))
        rewrite = f"https://{_HOST}/{shape.repo}"
        old[shape.id], new[shape.id] = _seed_two_commits(
            repositories,
            shape.skill,
            Path("skills") / shape.skill / "SKILL.md",
            shape.skill,
            source_tree=package.root,
            rewrite_url=rewrite,
            environment=environment,
        )

    mono_root = isolated.package_root / "sha-monorepo"
    mono_factory = LocalPackageFactory(mono_root / "packages")
    mono_package = mono_factory.create(_SUBDIR_PACKAGE, targets=("copilot",))
    mono_factory.add_skill(mono_package, _SUBDIR_PACKAGE, _skill_document(_SUBDIR_PACKAGE, "v1"))
    old["git-subdir"], new["git-subdir"] = _seed_two_commits(
        repositories,
        "sha-monorepo",
        Path("packages") / _SUBDIR_PACKAGE / "skills" / _SUBDIR_PACKAGE / "SKILL.md",
        _SUBDIR_PACKAGE,
        source_tree=mono_root,
        rewrite_url=f"https://{_HOST}/{_MONO_REPO}",
        environment=environment,
    )

    plugins: list[dict[str, object]] = []
    for shape in _SHAPES:
        for mode in ("sha-only", "ref-and-sha", "ref-only"):
            ref = None if mode == "sha-only" else "main"
            commit = None if mode == "ref-only" else old[shape.id]
            plugins.append(
                {
                    "name": f"{shape.id}-{mode}",
                    "description": f"{shape.id} {mode}",
                    "source": shape.source(commit, ref),
                }
            )
        for label, bad in _BAD_SHAS.items():
            plugins.append(
                {
                    "name": f"{shape.id}-bad-{label}",
                    "description": f"{shape.id} malformed sha",
                    "source": shape.source(bad, None),
                }
            )
    catalog_root = isolated.work_root / _MARKETPLACE_NAME
    (catalog_root / ".claude-plugin").mkdir(parents=True)
    (catalog_root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": _MARKETPLACE_NAME,
                "owner": {"name": "APM Sha Pin Tests"},
                "plugins": plugins,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return _Fixture(
        isolated=isolated,
        environment=environment,
        runner=ApmLifecycleRunner(
            (str(binary),), timeout_seconds=120, scenario_timeout_seconds=300
        ),
        repositories=repositories,
        catalog_root=catalog_root,
        old_commits=old,
        new_commits=new,
    )


@pytest.fixture(scope="module")
def fixture(
    tmp_path_factory: pytest.TempPathFactory,
    apm_binary_path: Path,
) -> _Fixture:
    """Build the shared hermetic repositories and catalog once per module."""
    return _create_fixture(tmp_path_factory.mktemp("sha-pin"), apm_binary_path)


def _install(
    fx: _Fixture,
    plugin: str,
    *,
    marketplace_dir: Path,
    marketplace_name: str,
    project_name: str,
) -> tuple[Path, CommandResult, ArtifactSnapshot]:
    """Add the marketplace to a fresh consumer and run the install."""
    env = dict(fx.environment)
    if plugin.startswith("github-"):
        # Offline stand-in for github.com: owner/repo sources resolve on this host.
        env["GITHUB_HOST"] = _HOST
    consumer = LocalPackageFactory(fx.isolated.work_root).create(project_name, targets=("copilot",))
    add = fx.runner.run(
        ("marketplace", "add", str(marketplace_dir), "--name", marketplace_name),
        scenario_id=f"sha-pin-add-{project_name}",
        cwd=consumer.root,
        env=env,
    )
    assert add.returncode == 0, _command_evidence(add)
    before = ArtifactSnapshot.capture(consumer.root)
    install = fx.runner.run(
        (
            "install",
            f"{plugin}@{marketplace_name}",
            "--target",
            "copilot",
            "--no-policy",
            "--verbose",
        ),
        scenario_id=f"sha-pin-install-{project_name}",
        cwd=consumer.root,
        env=env,
    )
    return consumer.root, install, before


def _assert_installed(
    root: Path,
    install: CommandResult,
    shape: _Shape,
    *,
    expected_sha: str,
    expected_body: str,
    pinned: bool,
) -> None:
    """Assert manifest, lock, deployment and output for one install."""
    assert install.returncode == 0, _command_evidence(install)
    output = (install.stdout + install.stderr).lower()
    if pinned:
        assert "unpinned" not in output, _command_evidence(install)
        manifest = load_yaml(root / "apm.yml")
        entries = manifest["dependencies"]["apm"]
        assert len(entries) == 1
        entry = entries[0]
        pin = entry["ref"] if isinstance(entry, dict) else entry.rpartition("#")[2]
        assert pin == expected_sha, entry
    lock = load_yaml(root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    assert dependencies[0]["resolved_commit"] == expected_sha
    skill = root / ".agents" / "skills" / shape.skill / "SKILL.md"
    assert expected_body in skill.read_text(encoding="utf-8")


@pytest.mark.parametrize("shape_id", [shape.id for shape in _SHAPES])
@pytest.mark.parametrize("mode", ["sha-only", "ref-and-sha"])
def test_sha_bearing_entry_installs_pinned_commit(
    fixture: _Fixture,
    shape_id: str,
    mode: str,
) -> None:
    """A sha (with or without ref) installs that commit, not the branch head."""
    shape = _SHAPE_BY_ID[shape_id]
    root, install, _ = _install(
        fixture,
        f"{shape_id}-{mode}",
        marketplace_dir=fixture.catalog_root,
        marketplace_name=_MARKETPLACE_NAME,
        project_name=f"consumer-{shape_id}-{mode}",
    )
    _assert_installed(
        root,
        install,
        shape,
        expected_sha=fixture.old_commits[shape_id].sha,
        expected_body="v1",
        pinned=True,
    )


@pytest.mark.parametrize("shape_id", [shape.id for shape in _SHAPES])
def test_ref_only_entry_still_resolves_branch_head(
    fixture: _Fixture,
    shape_id: str,
) -> None:
    """A ref-only entry keeps resolving the branch head."""
    shape = _SHAPE_BY_ID[shape_id]
    root, install, _ = _install(
        fixture,
        f"{shape_id}-ref-only",
        marketplace_dir=fixture.catalog_root,
        marketplace_name=_MARKETPLACE_NAME,
        project_name=f"consumer-{shape_id}-ref-only",
    )
    _assert_installed(
        root,
        install,
        shape,
        expected_sha=fixture.new_commits[shape_id].sha,
        expected_body="v2",
        pinned=False,
    )


@pytest.mark.parametrize("label", sorted(_BAD_SHAS))
@pytest.mark.parametrize("shape_id", [shape.id for shape in _SHAPES])
def test_malformed_sha_fails_before_project_writes(
    fixture: _Fixture,
    shape_id: str,
    label: str,
) -> None:
    """A malformed sha fails install without touching the project."""
    root, install, before = _install(
        fixture,
        f"{shape_id}-bad-{label}",
        marketplace_dir=fixture.catalog_root,
        marketplace_name=_MARKETPLACE_NAME,
        project_name=f"consumer-{shape_id}-bad-{label}",
    )
    assert install.returncode != 0, _command_evidence(install)
    assert "sha" in (install.stdout + install.stderr).lower(), _command_evidence(install)
    assert_unchanged(before, ArtifactSnapshot.capture(root))
    assert not (root / "apm.lock.yaml").exists()


def test_pack_copilot_commit_pin_round_trips_through_install(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A raw commit pin packed to a copilot catalog installs that commit."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    factory = LocalPackageFactory(isolated.package_root)
    shape = _Shape("url", "sha-pack-pkg", _PACK_REPO)
    package = factory.create(shape.skill, targets=("copilot",))
    factory.add_skill(package, shape.skill, _skill_document(shape.skill, "v1"))
    old, _new = _seed_two_commits(
        repositories,
        shape.skill,
        Path("skills") / shape.skill / "SKILL.md",
        shape.skill,
        source_tree=package.root,
        rewrite_url=f"https://{_HOST}/{_PACK_REPO}",
        environment=environment,
    )

    producer = LocalPackageFactory(isolated.work_root).create("pack-producer")
    manifest = load_yaml(producer.manifest_path)
    manifest["marketplace"] = {
        "owner": {"name": "APM Sha Pin Tests", "url": "https://example.invalid/sha-pin"},
        "outputs": {"copilot": {}},
        "packages": [
            {
                "name": shape.skill,
                "description": "Pinned package",
                "source": f"{_HOST}/{_PACK_REPO}",
                "ref": old.sha,
            }
        ],
    }
    dump_yaml(manifest, producer.manifest_path)

    runner = ApmLifecycleRunner(
        (str(apm_binary_path),), timeout_seconds=120, scenario_timeout_seconds=300
    )
    pack = runner.run(("pack",), scenario_id="sha-pin-pack", cwd=producer.root, env=environment)
    assert pack.returncode == 0, _command_evidence(pack)
    emitted = producer.root / ".github" / "plugin" / "marketplace.json"
    assert emitted.is_file(), _command_evidence(pack)
    plugins = json.loads(emitted.read_text(encoding="utf-8"))["plugins"]
    source = next(p["source"] for p in plugins if p["name"] == shape.skill)
    assert source["sha"] == old.sha
    assert "ref" not in source

    fx = _Fixture(
        isolated=isolated,
        environment=environment,
        runner=runner,
        repositories=repositories,
        catalog_root=producer.root,
        old_commits={},
        new_commits={},
    )
    root, install, _ = _install(
        fx,
        shape.skill,
        marketplace_dir=producer.root,
        marketplace_name="sha-pin-packed",
        project_name="consumer-packed",
    )
    _assert_installed(root, install, shape, expected_sha=old.sha, expected_body="v1", pinned=True)
