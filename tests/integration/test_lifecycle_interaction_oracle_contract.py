"""Fast adversarial oracle contracts; no installed binary or E2E prerequisite."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS
from tests.utils.lifecycle_interaction_oracle import (
    InteractionOracle,
    SourceFixture,
    assert_members_preserved,
    expected_routing,
)
from tests.utils.lifecycle_interactions import ROUTING_ROWS, RoutingRow

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]


def _oracle(tmp_path: Path) -> InteractionOracle:
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return InteractionOracle(
        {"project": project, "user": home},
        "project",
        project,
        (
            SourceFixture(
                "fixture", "prompts", "task", "expected-marker", (".apm/prompts/task.prompt.md",)
            ),
        ),
        RoutingRow("contract", ("prompts",), ("copilot",), False),
    )


@pytest.mark.parametrize(
    ("root_id", "relative"),
    (
        ("project", ".github/workflows/unrelated.yml"),
        ("user", ".config/unrelated-app/settings.json"),
        ("user", ".apm/unrelated.txt"),
        ("user", ".local/unrelated.txt"),
    ),
)
def test_exact_ancestors_reject_neighbor_overwrite(
    tmp_path: Path,
    root_id: str,
    relative: str,
) -> None:
    """A writable descendant never authorizes the rest of its parent tree."""
    oracle = _oracle(tmp_path)
    path = oracle.roots[root_id] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unowned")
    with pytest.raises(AssertionError, match="outside its declared write set"):
        oracle.observe(
            "corruption",
            lambda: path.write_bytes(b"corrupted"),
            exact={
                "project": {".github/prompts/task.prompt.md"},
                "user": {
                    ".config/opencode/commands/task.md",
                    ".apm/config.json",
                    ".local/state/apm/lock",
                },
            },
        )


def test_exact_ancestor_entries_allow_legitimate_creation(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    path = oracle.roots["project"] / ".github/prompts/task.prompt.md"

    def create() -> None:
        path.parent.mkdir(parents=True)
        path.write_text("expected-marker")

    oracle.observe("install", create, exact={"project": {".github/prompts/task.prompt.md"}})
    oracle.evaluated("outcome.status_matches_state")
    oracle.assert_finished({"install"})
    assert path.read_text() == "expected-marker"


def test_exact_ancestor_permission_cannot_create_a_file(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    with pytest.raises(AssertionError, match="Writable ancestor is not a directory"):
        oracle.observe(
            "corrupt-ancestor",
            lambda: (oracle.roots["project"] / ".github").write_bytes(b"not a directory"),
            exact={"project": {".github/prompts/task.prompt.md"}},
        )


def _materialize_expected(oracle: InteractionOracle, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = expected_routing(oracle.row, oracle.sources)
    for relative in expected.files:
        path = oracle.roots["project"] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("expected-marker", encoding="ascii")
    records = {
        str(index): SimpleNamespace(locator=SimpleNamespace(target=target, value=path))
        for index, (target, path) in enumerate(expected.ledger)
    }
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(deployment_ledger=SimpleNamespace(records=records)),
    )


def test_omitted_ledger_record_fails_even_when_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle, monkeypatch)
    oracle.assert_routing(("copilot",))
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(deployment_ledger=SimpleNamespace(records={})),
    )
    with pytest.raises(AssertionError, match="Ledger differs from source-derived routing"):
        oracle.assert_routing(("copilot",))


def test_wrong_target_claim_cannot_grant_write_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle, monkeypatch)
    unexpected = SimpleNamespace(
        locator=SimpleNamespace(target="cursor", value=".cursor/commands/task.md")
    )
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(
            deployment_ledger=SimpleNamespace(records={"wrong": unexpected})
        ),
    )
    with pytest.raises(AssertionError, match="Ledger differs from source-derived routing"):
        oracle.assert_routing(("copilot",))


def test_removed_target_leak_detected_before_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle, monkeypatch)
    oracle.assert_routing(("copilot",))
    leaked = ".cursor/commands/task.md"
    oracle.introduced.add(leaked)
    path = oracle.roots["project"] / leaked
    path.parent.mkdir(parents=True)
    path.write_text("stale widened deployment")
    with pytest.raises(AssertionError, match="Removed target leaked deployment"):
        oracle.assert_routing(("copilot",))


def test_never_authorized_target_without_ledger_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle, monkeypatch)
    forbidden = oracle.roots["project"] / ".cursor/commands/task.md"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("expected-marker")
    with pytest.raises(AssertionError, match="Removed target leaked deployment"):
        oracle.assert_routing(("copilot",))


def test_missing_required_file_cannot_be_excused_by_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle, monkeypatch)
    (oracle.roots["project"] / ".github/prompts/task.prompt.md").unlink()
    with pytest.raises(AssertionError, match="Missing required deployment"):
        oracle.assert_routing(("copilot",))


def test_final_cleanup_checks_lifetime_not_initial_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    leaked = ".cursor/commands/introduced-after-first-install.md"
    oracle.introduced.add(leaked)
    path = oracle.roots["project"] / leaked
    path.parent.mkdir(parents=True)
    path.write_text("late ownership")
    with pytest.raises(AssertionError, match="Removed target leaked deployment"):
        oracle.assert_routing(())


def test_shared_config_preservation_uses_catalog_not_filename_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle = _oracle(tmp_path)
    oracle.row = replace(oracle.row, primitives=("hooks",), targets=("claude",))
    oracle.sources = (replace(oracle.sources[0], primitive="hooks"),)
    monkeypatch.setitem(
        KNOWN_TARGETS,
        "claude",
        replace(KNOWN_TARGETS["claude"], hooks_config_display=".claude/hook-config.yaml"),
    )
    path = oracle.roots["project"] / ".claude/hook-config.yaml"
    path.parent.mkdir()
    path.write_text("user: keep\n", encoding="ascii")
    oracle.assert_routing(())
    path.write_text("hook: expected-marker\n", encoding="ascii")
    with pytest.raises(AssertionError, match="Removed target leaked shared ownership"):
        oracle.assert_routing(())


def test_source_provenance_rejects_swapped_dependency_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_primitive_target_covering_array import _assert_provenance

    oracle = _oracle(tmp_path)
    oracle.sources = (*oracle.sources, replace(oracle.sources[0], package_name="child"))
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(
            get_package_dependencies=lambda: (
                SimpleNamespace(name="fixture", resolved_commit="child-commit"),
                SimpleNamespace(name="child", resolved_commit="parent-commit"),
            )
        ),
    )
    with pytest.raises(AssertionError):
        _assert_provenance(oracle, {"fixture": "parent-commit", "child": "child-commit"})


def test_known_gap_does_not_allow_configuration_rewrites(tmp_path: Path) -> None:
    from tests.integration.test_primitive_target_covering_array import _assert_reinstall

    oracle = _oracle(tmp_path)
    oracle.row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    path = oracle.roots["user"] / ".apm/config.json"
    path.parent.mkdir()
    path.write_text('{"keep": true}\n', encoding="ascii")
    before = oracle.capture()
    path.write_text('{"keep": false}\n', encoding="ascii")
    with pytest.raises(AssertionError, match="outside its declared write set"):
        _assert_reinstall(oracle, before, None)


def test_skipped_transition_assertions_fail_closed(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    oracle.observe("narrow", lambda: None, unchanged=True)
    with pytest.raises(AssertionError, match="Skipped transition assertions: narrow"):
        oracle.observe("prune", lambda: None, unchanged=True)
    with pytest.raises(AssertionError, match="Skipped transition assertions: narrow"):
        oracle.assert_finished({"narrow", "prune"})


def test_deleted_transition_cannot_vacuously_finish(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    oracle.observe("install", lambda: None, unchanged=True)
    oracle.evaluated()
    with pytest.raises(AssertionError, match="Missing evaluated transitions"):
        oracle.assert_finished({"install", "widen", "narrow", "prune"})


@pytest.mark.parametrize(
    "corrupt",
    (
        {"foreign": {"keep": False}, "hooks": ["foreign-hook"]},
        {"foreign": {"keep": True}, "hooks": []},
        {"hooks": ["foreign-hook"]},
    ),
)
def test_shared_config_corruption_rejected_despite_exact_file_permission(
    tmp_path: Path,
    corrupt: dict,
) -> None:
    oracle = _oracle(tmp_path)
    path = oracle.roots["project"] / ".claude/settings.json"
    path.parent.mkdir()
    before = {"foreign": {"keep": True}, "hooks": ["foreign-hook"]}
    path.write_text(json.dumps(before))
    oracle.protected_json[path] = before
    with pytest.raises(AssertionError, match="Shared"):
        oracle.observe(
            "overwrite-shared",
            lambda: path.write_text(json.dumps(corrupt)),
            exact={"project": {".claude/settings.json"}},
        )


def test_shared_member_preservation_allows_another_owner() -> None:
    expected = {"hooks": ["surviving-package"], "user": {"keep": True}}
    actual = {"hooks": ["surviving-package", "new-package"], "user": {"keep": True}, "new": 1}
    assert_members_preserved(expected, actual)
    assert actual["hooks"] == ["surviving-package", "new-package"]


def test_failed_case_reports_partial_evidence_without_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed actions remain failures and never inherit the required-law inventory."""
    from tests.integration import test_primitive_target_covering_array as execution

    oracle = _oracle(tmp_path)
    emitted = []

    def fail_after_install(*arguments: object) -> None:
        observed = arguments[-2]
        assert isinstance(observed, list)
        observed.append(oracle)
        oracle.observe("install", lambda: None, unchanged=True)
        oracle.evaluated("outcome.status_matches_state")
        oracle.observe("audit", lambda: None, unchanged=True)
        raise AssertionError("deliberate failed action")

    monkeypatch.setattr(execution, "_execute_row", fail_after_install)
    with pytest.raises(AssertionError, match="deliberate failed action"):
        execution.execute_row(
            tmp_path,
            tmp_path / "unused-apm",
            oracle.row,
            record_execution=emitted.append,
        )
    assert len(emitted) == 1
    assert emitted[0].status == "failed"
    assert emitted[0].transitions == ("install", "audit")
    assert "idempotency.byte_stable" not in emitted[0].evaluated_laws
    assert "routing.authorized_targets_only" not in emitted[0].evaluated_laws
    assert json.loads((tmp_path / "lifecycle-execution.json").read_text())["status"] == "failed"
