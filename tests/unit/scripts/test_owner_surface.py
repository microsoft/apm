"""Owner-surface PR check: which changes trigger it and which tests it keeps."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import owner_surface
from tests.workflow_contracts import load_workflow, run_bash, workflow_job, workflow_step

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]
pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/owner-surface.yml"
LOCK_OWNER = {
    "id": "read-only-lockfile-path",
    "selectors": ["src/apm_cli/deps/lockfile.py"],
    "guards": ["contracts-tooling-lockfile-read"],
}
HOOK_OWNER = {
    "id": "hook-merge",
    "selectors": ["src/apm_cli/integration/hook_*.py"],
    "guards": ["hooks-merge-guard"],
}
OWNERS = {owner["id"]: owner for owner in (LOCK_OWNER, HOOK_OWNER)}
RULES = [
    ("contracts-tooling-lockfile-read", ("contracts-tooling-lockfile-read",), "scripts/a/lock.py"),
    ("hooks-merge-rule", ("hooks-merge-guard",), "scripts/architecture_linter/checks/hooks.py"),
]


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["src/apm_cli/install/services.py"], True),
        (["src/apm_cli/deps/lockfile_lookup.py"], True),
        ([".apm/architecture/owners/contracts-tooling.json"], True),
        (["scripts/architecture_linter/checks/hooks.py"], True),
        (["tests/integration/test_install_local_bundle_e2e.py"], True),
        # A file an owner selector names is an owner surface wherever it lives.
        (["src/apm_cli/integration/hook_integrator.py"], True),
        (["docs/src/content/docs/index.md", "CHANGELOG.md"], False),
        (["src/apm_cli/commands/view.py"], False),
    ],
)
def test_surface_covers_owner_and_install_paths(changed: list[str], expected: bool) -> None:
    assert owner_surface.on_surface(changed, OWNERS) is expected


def test_a_changed_registry_entry_selects_its_owner_guard_and_rule() -> None:
    """#4067: adding a selector to an owner must reach the tests that pin that owner."""
    widened = {**LOCK_OWNER, "selectors": [*LOCK_OWNER["selectors"], "src/apm_cli/deps/x.py"]}
    keys = owner_surface.selection_keys(
        [".apm/architecture/owners/contracts-tooling.json", "CHANGELOG.md"],
        {**OWNERS, LOCK_OWNER["id"]: widened},
        OWNERS,
        RULES,
    )
    assert {"read-only-lockfile-path", "contracts-tooling-lockfile-read"} <= keys
    assert "hook-merge" not in keys and "CHANGELOG.md" not in keys


def test_a_changed_source_selects_its_module_owner_and_rule() -> None:
    keys = owner_surface.selection_keys(
        ["src/apm_cli/integration/hook_integrator.py"], OWNERS, OWNERS, RULES
    )
    assert {
        "src/apm_cli/integration/hook_integrator.py",
        "apm_cli.integration.hook_integrator",
        "hook-merge",
        "hooks-merge-guard",
        "hooks-merge-rule",
    } <= keys
    assert "read-only-lockfile-path" not in keys


def test_a_changed_rule_module_selects_its_rule_and_owner() -> None:
    keys = owner_surface.selection_keys(
        ["scripts/architecture_linter/checks/hooks.py"], OWNERS, OWNERS, RULES
    )
    assert {"hooks-merge-rule", "hooks-merge-guard", "hook-merge"} <= keys


def test_a_removed_owner_is_affected() -> None:
    assert owner_surface.affected_owners([], {}, OWNERS) == set(OWNERS)


@dataclass(frozen=True)
class _Case:
    guard_id: str
    path: str


def _item(path: str, *, params: dict | None = None, function=None) -> SimpleNamespace:
    item = SimpleNamespace(path=ROOT / path, function=function)
    if params is not None:
        item.callspec = SimpleNamespace(params=params)
    return item


def _structural_check() -> None:
    assert sorted([1]) == [1]


def _rule_check() -> None:
    owner_surface.run_selected_rules(ROOT, ("other-rule",))  # type: ignore[attr-defined]


SELECTION = {
    "keys": ["hooks-merge-guard", "apm_cli.integration.hook_integrator"],
    "changed": ["tests/integration/test_architecture_changed.py"],
    "install": False,
}


@pytest.mark.parametrize(
    ("item", "texts", "kept"),
    [
        (
            _item(
                owner_surface.MUTATION_TESTS,
                params={"case": _Case("hooks-merge-guard", "src/apm_cli/x.py")},
            ),
            {},
            True,
        ),
        (
            _item(
                owner_surface.MUTATION_TESTS,
                params={"case": _Case("other-guard", "src/apm_cli/x.py")},
            ),
            {},
            False,
        ),
        (_item(owner_surface.MUTATION_TESTS, function=_structural_check), {}, True),
        (_item(owner_surface.MUTATION_TESTS, function=_rule_check), {}, False),
        (
            _item("tests/integration/test_architecture_hooks.py"),
            {
                "tests/integration/test_architecture_hooks.py": "from apm_cli.integration.hook_integrator import x"
            },
            True,
        ),
        (
            _item("tests/integration/test_architecture_other.py"),
            {"tests/integration/test_architecture_other.py": "OWNER = 'unrelated'"},
            False,
        ),
        (_item("tests/integration/test_architecture_changed.py"), {}, True),
    ],
)
def test_keep_item_selects_only_tests_that_name_a_key(
    item: SimpleNamespace, texts: dict[str, str], kept: bool
) -> None:
    assert owner_surface.keep_item(item, SELECTION, texts.__getitem__) is kept


def test_install_changes_keep_every_install_integration_file() -> None:
    item = _item("tests/integration/test_install_local_bundle_e2e.py")
    selection = {**SELECTION, "install": True}
    assert owner_surface.keep_item(item, selection, {}.__getitem__)


def test_plugin_is_inert_without_a_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(owner_surface.SELECTION_ENV, raising=False)
    items = [object()]
    owner_surface.pytest_collection_modifyitems(SimpleNamespace(), items)
    assert len(items) == 1


def test_plugin_deselects_items_outside_the_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = tmp_path / "selection.json"
    location.write_text(json.dumps({**SELECTION, "keys": ["hooks-merge-guard"]}), encoding="utf-8")
    monkeypatch.setenv(owner_surface.SELECTION_ENV, str(location))
    keep = _item(owner_surface.MUTATION_TESTS, params={"case": _Case("hooks-merge-guard", "p")})
    drop = _item(owner_surface.MUTATION_TESTS, params={"case": _Case("other", "p")})
    deselected: list[object] = []
    config = SimpleNamespace(
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    )
    items = [keep, drop]
    owner_surface.pytest_collection_modifyitems(config, items)
    assert items == [keep] and deselected == [drop]


def test_workflow_always_reports_a_stable_check() -> None:
    """No paths filter, PR and merge_group triggers, one always() aggregate."""
    workflow = load_workflow(WORKFLOW)
    triggers = workflow["on"]
    assert set(triggers) == {"pull_request", "merge_group"}
    for config in triggers.values():
        assert not {"paths", "paths-ignore"} & set(config or {})
    gate = workflow_job(workflow, "owner-surface")
    assert gate["name"] == "Owner Surface Check"
    assert gate["if"] == "always()" and gate["needs"] == ["select", "slice"]
    assert workflow_job(workflow, "slice")["if"] == "needs.select.outputs.run == 'true'"


@pytest.mark.parametrize(
    ("select", "run", "slice_result", "passes"),
    [
        ("success", "false", "skipped", True),
        ("success", "true", "success", True),
        ("success", "true", "failure", False),
        ("success", "true", "skipped", False),
        ("success", "true", "cancelled", False),
        ("success", "false", "success", False),
        ("failure", "", "skipped", False),
        ("cancelled", "", "skipped", False),
    ],
)
def test_aggregate_passes_on_skipped_or_green_slice_only(
    select: str, run: str, slice_result: str, passes: bool
) -> None:
    step = workflow_step(
        workflow_job(load_workflow(WORKFLOW), "owner-surface"), "Require the selected slice"
    )
    env = {"SELECT_RESULT": select, "RUN": run, "SLICE_RESULT": slice_result}
    assert (run_bash(step["run"], env=env).returncode == 0) is passes


def test_plan_lists_changes_and_writes_the_run_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(owner_surface, "changed_paths", lambda base, root: ["docs/x.md"])
    changed = tmp_path / "changed.txt"
    assert owner_surface.plan("base", changed) is False
    assert changed.read_text(encoding="utf-8") == "docs/x.md\n"
    assert output.read_text(encoding="utf-8") == "run=false\n"


def test_a_slice_too_small_to_split_runs_whole_on_shard_one(pytester: pytest.Pytester) -> None:
    """scripts.ci_sharding refuses empty shards; surplus owner-surface shards run nothing."""
    pytester.makepyfile(test_tiny="def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    items, _ = pytester.inline_genitems()
    shards = [owner_surface.shard_items(items, index, 3, None) for index in (1, 2, 3)]
    assert sorted(len(shard) for shard in shards) == [0, 1, 1]
    assert {item.nodeid for shard in shards for item in shard} == {item.nodeid for item in items}
