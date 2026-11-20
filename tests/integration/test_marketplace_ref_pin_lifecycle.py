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

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_HOST = "gitlab.example.invalid"
_REMOTE = f"https://{_HOST}/team/catalog"
_ALIAS = "company"
_PLUGIN = "example-plugin"
_BROKEN = "broken-plugin"
_FLAGS = ("--target", "codex", "--parallel-downloads", "0")
_PIN_KEY = f"{_ALIAS}/{_PLUGIN}"


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
