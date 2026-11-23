"""Real-CLI marketplace ref pins are recorded only by a committed install.

PROMISE: I5 / I7 -- a read-only or failed ``apm install`` of
``plugin@marketplace`` writes nothing under APM_HOME; only a committed install
records the plugin's ref in ``cache/marketplace/version-pins.json``
(source: #4212, #4194; src/apm_cli/install/transaction.py).
CELL: OP=install SRC=marketplace(git-subdir) SCOPE=project DRYRUN|FAILED
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.source_identity import catalog_pin_identity
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]

_HOST = "gitlab.example.invalid"
_REMOTE = f"https://{_HOST}/team/catalog"
_ALIAS = "company"
_PLUGIN = "example-plugin"
_BROKEN = "broken-plugin"
_GHOST = "ghost-plugin"
_FLAGS = ("--target", "codex", "--parallel-downloads", "0")
_CATALOG_PATH = ".claude-plugin/marketplace.json"


def _catalog_key(remote: str) -> str:
    """The pin scope of a catalog: its origin and path, never the alias (#4966)."""
    return catalog_pin_identity(MarketplaceSource(name=_ALIAS, url=remote, path=_CATALOG_PATH))


_PIN_KEY = f"{_catalog_key(_REMOTE)}#{_PLUGIN}"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _plugin_entry(name: str, path: str) -> dict[str, object]:
    return {
        "name": name,
        "source": {"source": "git-subdir", "url": _REMOTE, "path": path, "ref": "main"},
    }


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "isolated", base_env=dict(os.environ)
        )
        repositories = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        repository = repositories.create("catalog")
        packages = LocalPackageFactory(repository.worktree / "plugins")
        package = packages.create(_PLUGIN, targets=("codex",))
        packages.add_skill(package, _PLUGIN, f"---\nname: {_PLUGIN}\ndescription: d\n---\nBody\n")
        catalog_dir = repository.worktree / ".claude-plugin"
        catalog_dir.mkdir()
        (catalog_dir / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "catalog",
                    "owner": {"name": "APM Test"},
                    "plugins": [
                        _plugin_entry(_PLUGIN, f"plugins/{_PLUGIN}"),
                        # #4194: the component path does not exist in the repository.
                        _plugin_entry(_BROKEN, "plugins/does-not-exist"),
                        # #4322: a source that validation cannot reach after its pin resolves.
                        {
                            "name": _GHOST,
                            "source": {
                                "source": "git-subdir",
                                "url": f"https://{_HOST}/team/ghost",
                                "path": "plugins/ghost",
                                "ref": "main",
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        repositories.commit(repository, message="publish catalog")
        self.env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
        self.apm_home = isolated.config_root
        self.pins = isolated.config_root / "cache" / "marketplace" / "version-pins.json"
        self.project = (
            LocalPackageFactory(isolated.work_root).create("consumer", targets=("codex",)).root
        )
        self._runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300)
        self.apm("marketplace", "add", _REMOTE, "--name", _ALIAS, "--ref", "main")

    def apm(self, *args: str, expected: int = 0) -> CommandResult:
        result = self._runner.run(args, cwd=self.project, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == expected, _evidence(result)
        return result

    def runner_run(self, *args: str) -> CommandResult:
        result = self._runner.run(args, cwd=self.project, env=self.env)
        _assert_output_oracles(result)
        return result

    def snapshot(self) -> tuple[dict[str, bytes], dict[str, bytes]]:
        return _snapshot(self.project), _snapshot(self.apm_home)


def test_install_dry_run_of_marketplace_plugin_writes_no_ref_pin(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4212: dry-run records no pin; the real install then records it."""
    world = _World(tmp_path, apm_binary_path)
    # Warm the catalog cache so the dry run has no legitimate cache write.
    world.apm("marketplace", "update", _ALIAS)
    assert not world.pins.exists()
    before = world.snapshot()

    preview = world.apm("install", "--dry-run", f"{_PLUGIN}@{_ALIAS}", *_FLAGS)

    assert "Dry run mode" in preview.stdout, _evidence(preview)
    assert not world.pins.exists(), _evidence(preview)
    assert world.snapshot() == before, "I5: install --dry-run changed durable state"

    world.apm("install", f"{_PLUGIN}@{_ALIAS}", *_FLAGS)
    assert json.loads(world.pins.read_text(encoding="utf-8")) == {_PIN_KEY: "main"}


def test_failed_marketplace_install_writes_no_ref_pin(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4194: a failed install preserves APM_HOME and the project."""
    world = _World(tmp_path, apm_binary_path)
    world.apm("marketplace", "update", _ALIAS)
    before = world.snapshot()

    failed = world.apm("install", f"{_BROKEN}@{_ALIAS}", *_FLAGS, expected=1)

    assert not world.pins.exists(), _evidence(failed)
    assert world.snapshot() == before, "I7: a failed install changed durable state"


def test_mixed_success_marketplace_install_pins_only_the_installed_plugin(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4322: a partly failed install records pins only for plugins that installed."""
    world = _World(tmp_path, apm_binary_path)
    world.apm("marketplace", "update", _ALIAS)
    assert not world.pins.exists()

    result = world.runner_run("install", f"{_PLUGIN}@{_ALIAS}", f"{_GHOST}@{_ALIAS}", *_FLAGS)

    assert result.returncode in (0, 1), _evidence(result)
    assert (world.project / ".agents" / "skills" / _PLUGIN / "SKILL.md").is_file(), _evidence(
        result
    )
    assert json.loads(world.pins.read_text(encoding="utf-8")) == {_PIN_KEY: "main"}, _evidence(
        result
    )


def _catalog_repository(repositories: LocalGitRepositoryFactory, name: str, remote: str, ref: str):
    repository = repositories.create(name)
    packages = LocalPackageFactory(repository.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    packages.add_skill(package, _PLUGIN, f"---\nname: {_PLUGIN}\ndescription: d\n---\nBody\n")
    (repository.worktree / ".claude-plugin").mkdir()
    _publish(repositories, repository, remote, ref)
    return repository


def _publish(repositories: LocalGitRepositoryFactory, repository, remote: str, ref: str) -> None:
    entry = _plugin_entry(_PLUGIN, f"plugins/{_PLUGIN}")
    entry["source"] = {**entry["source"], "url": remote, "ref": ref}
    (repository.worktree / _CATALOG_PATH).write_text(
        json.dumps({"name": "catalog", "owner": {"name": "APM Test"}, "plugins": [entry]}),
        encoding="utf-8",
    )
    commit = repositories.commit(repository, message=f"publish {ref}")
    if ref != "main":
        repositories.tag(repository, ref, commit)


class _TwoCatalogs:
    """Two catalogs that list the same plugin and version, and two consumer projects."""

    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "isolated", base_env=dict(os.environ)
        )
        self.repositories = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        self.remote_b = f"https://{_HOST}/other/catalog"
        self.catalog = _catalog_repository(self.repositories, "catalog", _REMOTE, "main")
        self.env = self.repositories.url_rewrite_subprocess_env_many(
            (
                (self.catalog, _REMOTE),
                (
                    _catalog_repository(self.repositories, "catalog-b", self.remote_b, "v1"),
                    self.remote_b,
                ),
            )
        )
        self.pins = isolated.config_root / "cache" / "marketplace" / "version-pins.json"
        projects = LocalPackageFactory(isolated.work_root)
        self.first = projects.create("first", targets=("codex",)).root
        self.second = projects.create("second", targets=("codex",)).root
        self._runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300)

    def apm(self, cwd: Path, *args: str, expected: int = 0) -> CommandResult:
        result = self._runner.run(args, cwd=cwd, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == expected, _evidence(result)
        return result

    def pinned(self) -> dict[str, str]:
        return json.loads(self.pins.read_text(encoding="utf-8"))


def test_removed_alias_re_added_for_another_catalog_installs_without_its_old_pins(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4966 (1): the catalog an alias names now owns the pins, not the alias."""
    world = _TwoCatalogs(tmp_path, apm_binary_path)
    world.apm(world.first, "marketplace", "add", _REMOTE, "--name", _ALIAS, "--ref", "main")
    world.apm(world.first, "install", f"{_PLUGIN}@{_ALIAS}", *_FLAGS)
    assert world.pinned() == {_PIN_KEY: "main"}

    world.apm(world.first, "marketplace", "remove", _ALIAS, "--yes")
    assert world.pinned() == {_PIN_KEY: "main"}

    world.apm(world.second, "marketplace", "add", world.remote_b, "--name", _ALIAS, "--ref", "main")
    world.apm(world.second, "install", f"{_PLUGIN}@{_ALIAS}", *_FLAGS)

    assert (world.second / ".agents" / "skills" / _PLUGIN / "SKILL.md").is_file()
    assert world.pinned() == {_PIN_KEY: "main", f"{_catalog_key(world.remote_b)}#{_PLUGIN}": "v1"}


def test_removing_and_re_adding_a_catalog_still_refuses_its_changed_ref(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4966 (2): removing a marketplace does not weaken pinning for its catalog."""
    world = _TwoCatalogs(tmp_path, apm_binary_path)
    world.apm(world.first, "marketplace", "add", _REMOTE, "--name", _ALIAS, "--ref", "main")
    world.apm(world.first, "install", f"{_PLUGIN}@{_ALIAS}", *_FLAGS)
    world.apm(world.first, "marketplace", "remove", _ALIAS, "--yes")
    _publish(world.repositories, world.catalog, _REMOTE, "v2")

    world.apm(world.second, "marketplace", "add", _REMOTE, "--name", "renamed", "--ref", "main")
    refused = world.apm(world.second, "install", f"{_PLUGIN}@renamed", *_FLAGS, expected=1)

    assert "ref changed: was 'main', now 'v2'" in refused.stdout + refused.stderr
    assert world.pinned() == {_PIN_KEY: "main"}
    assert not (world.second / ".agents" / "skills" / _PLUGIN).exists()


def test_a_legacy_alias_pin_migrates_to_its_catalog(tmp_path: Path, apm_binary_path: Path) -> None:
    """#4966 (3): an alias-keyed pin from an older APM keeps guarding the same catalog."""
    world = _TwoCatalogs(tmp_path, apm_binary_path)
    world.apm(world.first, "marketplace", "add", _REMOTE, "--name", _ALIAS, "--ref", "main")
    world.pins.parent.mkdir(parents=True, exist_ok=True)
    world.pins.write_text(json.dumps({f"{_ALIAS}/{_PLUGIN}": "v0"}), encoding="utf-8")

    refused = world.apm(world.first, "install", f"{_PLUGIN}@{_ALIAS}", *_FLAGS, expected=1)
    assert "ref changed: was 'v0', now 'main'" in refused.stdout + refused.stderr

    world.apm(world.first, "marketplace", "remove", _ALIAS, "--yes")
    assert world.pinned() == {_PIN_KEY: "v0"}
