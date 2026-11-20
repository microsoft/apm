"""Real-CLI marketplace ref swap: a changed ref is refused, never re-recorded (#4400).

A committed install records the plugin's ref. When the catalog later points
the same plugin version at another ref, every install refuses (exit 1),
names both refs, and leaves the recorded pin and the project unchanged, at
project scope and with ``-g``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.integration.test_marketplace_ref_pin_lifecycle import (
    _ALIAS,
    _FLAGS,
    _PIN_KEY,
    _PLUGIN,
    _REMOTE,
    _assert_output_oracles,
    _evidence,
    _plugin_entry,
    _snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-marketplace-ref-pin-commit", "P-INV-I7"),
]


class _SwapWorld:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "isolated", base_env=dict(os.environ)
        )
        self._repositories = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        self._repository = self._repositories.create("catalog")
        packages = LocalPackageFactory(self._repository.worktree / "plugins")
        package = packages.create(_PLUGIN, targets=("codex",))
        packages.add_skill(package, _PLUGIN, f"---\nname: {_PLUGIN}\ndescription: d\n---\nBody\n")
        self._publish("main")
        self.env = self._repositories.url_rewrite_subprocess_env(self._repository, _REMOTE)
        self.apm_home = isolated.config_root
        self.pins = isolated.config_root / "cache" / "marketplace" / "version-pins.json"
        self._consumers = LocalPackageFactory(isolated.work_root)
        self._consumer = self._consumers.create("consumer", targets=("codex",))
        self.project = self._consumer.root
        self._runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300)
        self.apm("marketplace", "add", _REMOTE, "--name", _ALIAS, "--ref", "main")

    def _publish(self, ref: str) -> str:
        catalog_dir = self._repository.worktree / ".claude-plugin"
        catalog_dir.mkdir(exist_ok=True)
        entry = _plugin_entry(_PLUGIN, f"plugins/{_PLUGIN}")
        entry["source"]["ref"] = ref  # type: ignore[index]
        (catalog_dir / "marketplace.json").write_text(
            json.dumps({"name": "catalog", "owner": {"name": "APM Test"}, "plugins": [entry]}),
            encoding="utf-8",
        )
        return self._repositories.commit(self._repository, message=f"publish {ref}").sha

    def swap_ref(self) -> str:
        """Point the catalog's plugin entry at a new commit with changed content."""
        skill = self._repository.worktree / "plugins" / _PLUGIN
        for path in skill.rglob("SKILL.md"):
            path.write_text(path.read_text(encoding="utf-8") + "Swapped\n", encoding="utf-8")
        head = self._repositories.commit(self._repository, message="swap content").sha
        self._publish(head)
        self.apm("marketplace", "update", _ALIAS)
        return head

    def declare_marketplace_dependency(self) -> None:
        self._consumers.replace_apm_dependencies(
            self._consumer, ({"name": _PLUGIN, "marketplace": _ALIAS},)
        )

    def apm(self, *args: str, expected: int = 0) -> CommandResult:
        result = self._runner.run(args, cwd=self.project, env=self.env)
        _assert_output_oracles(result)
        assert result.returncode == expected, _evidence(result)
        return result

    def runner_result(self, *args: str) -> CommandResult:
        result = self._runner.run(args, cwd=self.project, env=self.env)
        _assert_output_oracles(result)
        return result

    def snapshot(self) -> tuple[dict[str, bytes], dict[str, bytes]]:
        return _snapshot(self.project), _snapshot(self.apm_home)


@pytest.mark.parametrize("scope", [(), ("-g",)], ids=["project", "user"])
def test_swapped_marketplace_ref_is_refused_every_time(
    tmp_path: Path, apm_binary_path: Path, scope: tuple[str, ...]
) -> None:
    world = _SwapWorld(tmp_path, apm_binary_path)
    world.apm("install", *scope, f"{_PLUGIN}@{_ALIAS}", *_FLAGS)
    assert json.loads(world.pins.read_text(encoding="utf-8")) == {_PIN_KEY: "main"}

    swapped = world.swap_ref()
    before = world.snapshot()
    for _attempt in range(2):
        refused = world.apm("install", *scope, f"{_PLUGIN}@{_ALIAS}", *_FLAGS, expected=1)
        output = " ".join((refused.stdout + refused.stderr).split())
        assert f"ref changed: was 'main', now '{swapped}'" in output, _evidence(refused)
        assert "refusing to install" in output, _evidence(refused)
        assert "run 'apm update' to accept the new ref" in output, _evidence(refused)
        assert json.loads(world.pins.read_text(encoding="utf-8")) == {_PIN_KEY: "main"}
        assert world.snapshot() == before, "I7: a refused install changed durable state"


@pytest.mark.parametrize("selection", [(), (f"{_PLUGIN}@{_ALIAS}",)], ids=["all", "selected"])
def test_update_accepts_a_swapped_ref_visibly_and_frozen_audit_agree(
    tmp_path: Path, apm_binary_path: Path, selection: tuple[str, ...]
) -> None:
    world = _SwapWorld(tmp_path, apm_binary_path)
    world.declare_marketplace_dependency()
    world.apm("install", *_FLAGS)
    assert json.loads(world.pins.read_text(encoding="utf-8")) == {_PIN_KEY: "main"}

    swapped = world.swap_ref()
    before = world.snapshot()
    frozen = world.runner_result("install", "--frozen", *_FLAGS)
    audit = world.runner_result("audit", "--ci", "--no-policy")
    assert (frozen.returncode, audit.returncode) == (0, 0), _evidence(frozen) + _evidence(audit)
    assert world.snapshot() == before, "the locked replay moved state on a re-pinned catalog"

    updated = world.apm("update", *selection, "--yes", *_FLAGS)
    output = " ".join((updated.stdout + updated.stderr).split())
    assert (
        f"Plugin {_PLUGIN}@{_ALIAS} ref changed: was 'main', now '{swapped}'. "
        "Accepted by this update." in output
    ), _evidence(updated)
    assert json.loads(world.pins.read_text(encoding="utf-8")) == {_PIN_KEY: swapped}
