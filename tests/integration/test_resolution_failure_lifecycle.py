"""Installed Python CLI staging controls and real non-admin Windows denials."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from filelock import FileLock

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from scripts.windows_native_symlink_probe_entry import probe_native_context
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _new_scenario,
    _publish,
    _PublishedPackage,
    _Scenario,
)
from tests.unit.install import test_resolution_failure_recovery as recovery
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.local_package import LocalPackage
from tests.utils.resolution_failure_probe import hold_path, release_directory

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke]


@pytest.mark.requires_windows_native_standard_user
@pytest.mark.parametrize("parent_first", [False, True], ids=["child-first", "parent-first"])
@pytest.mark.parametrize("child_existed", [True, False], ids=["prior-child", "new-child"])
def test_native_nested_replacement_retry_preserves_original_child_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_first: bool,
    child_existed: bool,
    record_property: Callable[[str, object], None],
) -> None:
    """Run actual locked-backup cases behind the canonical native capability gate."""
    recovery.test_nested_replacement_denied_restore_retry_preserves_original_child_bytes(
        tmp_path,
        monkeypatch,
        parent_first,
        child_existed,
        record_property,
        native_denial=True,
    )


@pytest.fixture
def scenario(tmp_path: Path, apm_engine_command: tuple[str, ...]) -> _Scenario:
    """Use the installed module entry point with the existing hermetic factories."""
    initial = _new_scenario(tmp_path / "staging", Path(apm_engine_command[0]))
    # Match the report's Git setting only in this scenario's owned config files.
    for config in (initial.isolated.root / "gitconfig", initial.isolated.home / ".gitconfig"):
        with config.open("a", encoding="utf-8") as handle:
            handle.write("[core]\n\tlongpaths = true\n")
    return replace(initial, runner=ApmLifecycleRunner(apm_engine_command))


def _run(scenario: _Scenario, consumer: LocalPackage, env: dict[str, str]) -> CommandResult:
    return scenario.runner.run(
        (*_INSTALL_ARGS, "--target", "copilot", "--verbose"),
        cwd=consumer.root,
        env={**env, "COLUMNS": "300"},
        scenario_id="resolution-staging-recovery",
    )


def _success(result: CommandResult) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Git clone target is not empty" not in result.stdout + result.stderr


def _publish_staging(scenario: _Scenario) -> _PublishedPackage:
    source = _publish(scenario, "staging-kit", instruction="staging")
    return replace(
        source,
        dependency={key: value for key, value in source.dependency.items() if key != "alias"},
    )


def _advance(scenario: _Scenario, source: _PublishedPackage, consumer: LocalPackage) -> str:
    (source.repository.worktree / "revision.txt").write_bytes(b"replacement revision\n")
    try:
        commit = scenario.repositories.commit(source.repository, message="replacement revision")
    except subprocess.CalledProcessError as error:
        pytest.fail(f"Fixture commit failed: {error.stderr}")
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"][0]["ref"] = commit.sha
    dump_yaml(manifest, consumer.manifest_path)
    return commit.sha


def _assert_pin(consumer: LocalPackage, sha: str) -> None:
    lock = load_yaml(consumer.root / "apm.lock.yaml")
    assert len(lock["dependencies"]) == 1
    assert lock["dependencies"][0]["resolved_commit"] == sha


def _live(consumer: LocalPackage) -> Path:
    return consumer.root / "apm_modules" / "apm-fixture-org" / "staging-kit"


@pytest.mark.parametrize(
    "existing_parent", [False, True], ids=["missing-parent", "existing-parent"]
)
def test_module_first_install_and_pinned_replacement(
    scenario: _Scenario, existing_parent: bool
) -> None:
    """The reported missing-parent shape already succeeds on current main."""
    source = _publish_staging(scenario)
    consumer = scenario.consumers.create("consumer", dependencies=(source.dependency,))
    live = _live(consumer)
    if existing_parent:
        live.parent.mkdir(parents=True)
    user_file = consumer.root / "user.txt"
    user_file.write_bytes(b"unrelated user bytes\r\n")
    _success(_run(scenario, consumer, source.environment))
    _assert_pin(consumer, source.commit.sha)
    assert (live / "apm.yml").read_bytes() == source.package.manifest_path.read_bytes()
    long_leaf = "long-" + "x" * 110 + ".txt"
    (source.repository.worktree / long_leaf).write_bytes(b"long path payload")
    sha = _advance(scenario, source, consumer)
    _success(_run(scenario, consumer, source.environment))
    _assert_pin(consumer, sha)
    assert (live / "revision.txt").read_bytes() == b"replacement revision\n"
    long_path = live / long_leaf
    assert long_path.read_bytes() == b"long path payload"
    if os.name == "nt":
        assert len(str(long_path)) > 260
    assert user_file.read_bytes() == b"unrelated user bytes\r\n"
    assert not (consumer.root / "apm_modules" / ".apm-resolution-staging").exists()


def test_parent_obstruction_preserves_state_and_retry_uses_clean_candidate(
    scenario: _Scenario,
) -> None:
    """A real filesystem obstruction fails without a fabricated missing mkdir."""
    source = _publish_staging(scenario)
    consumer = scenario.consumers.create("consumer", dependencies=(source.dependency,))
    manifest = consumer.manifest_path.read_bytes()
    live = _live(consumer)
    live.parent.parent.mkdir()
    live.parent.write_bytes(b"obstructing file")
    result = _run(scenario, consumer, source.environment)
    assert result.returncode == 1, result.stdout + result.stderr
    assert live.parent.read_bytes() == b"obstructing file"
    assert consumer.manifest_path.read_bytes() == manifest
    assert not (consumer.root / "apm.lock.yaml").exists()
    live.parent.unlink()
    _success(_run(scenario, consumer, source.environment))
    _assert_pin(consumer, source.commit.sha)


def test_install_preserves_active_unknown_and_legacy_staging(scenario: _Scenario) -> None:
    """A real installed CLI may clean only inactive, lock-aware owned roots."""
    source = _publish_staging(scenario)
    consumer = scenario.consumers.create("consumer", dependencies=(source.dependency,))
    staging = consumer.root / "apm_modules" / ".apm-resolution-staging"
    active, legacy, unknown = (staging / name for name in ("a" * 12, "b" * 32, "unknown"))
    for root in (active, legacy, unknown):
        root.mkdir(parents=True)
        (root / "marker").write_bytes(b"must survive")
    with FileLock(str(active.with_suffix(".lock"))).acquire(timeout=0):
        _success(_run(scenario, consumer, source.environment))
        for root in (active, legacy, unknown):
            assert (root / "marker").read_bytes() == b"must survive"


def test_invalid_download_never_replaces_prior_validated_package(scenario: _Scenario) -> None:
    """A real pinned invalid Git package is rejected before activating its bytes."""
    source = _publish_staging(scenario)
    consumer = scenario.consumers.create("consumer", dependencies=(source.dependency,))
    _success(_run(scenario, consumer, source.environment))
    live = _live(consumer)
    prior = (live / "apm.yml").read_bytes()
    lock = (consumer.root / "apm.lock.yaml").read_bytes()
    (source.repository.worktree / "apm.yml").write_bytes(b"name: [invalid\n")
    _advance(scenario, source, consumer)
    manifest = consumer.manifest_path.read_bytes()
    result = _run(scenario, consumer, source.environment)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (live / "apm.yml").read_bytes() == prior
    assert not (live / "revision.txt").exists()
    assert consumer.manifest_path.read_bytes() == manifest
    assert (consumer.root / "apm.lock.yaml").read_bytes() == lock
    assert not (live.parents[1] / ".apm-resolution-staging").exists()


@pytest.mark.requires_windows_native_standard_user
def test_native_locked_destination_preserves_prior_install_and_recovers(
    scenario: _Scenario, record_property: Callable[[str, object], None]
) -> None:
    """An actual no-delete-share destination handle denies backup, then clears."""
    context = probe_native_context(
        scenario.isolated.root / "native-context", os.environ["APM_WINDOWS_NATIVE_EXPECTED_SID"]
    )
    record_property("native_context", json.dumps(context, sort_keys=True))
    source = _publish_staging(scenario)
    consumer = scenario.consumers.create("consumer", dependencies=(source.dependency,))
    _success(_run(scenario, consumer, source.environment))
    live = _live(consumer)
    prior = (live / "apm.yml").read_bytes()
    old_lock = (consumer.root / "apm.lock.yaml").read_bytes()
    sha = _advance(scenario, source, consumer)
    manifest = consumer.manifest_path.read_bytes()
    held = hold_path(live)
    try:
        result = _run(scenario, consumer, source.environment)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "WinError" in result.stdout + result.stderr
        assert (live / "apm.yml").read_bytes() == prior
        assert consumer.manifest_path.read_bytes() == manifest
        assert (consumer.root / "apm.lock.yaml").read_bytes() == old_lock
    finally:
        release_directory(held)
    _success(_run(scenario, consumer, source.environment))
    _assert_pin(consumer, sha)


@pytest.mark.requires_windows_native_standard_user
@pytest.mark.parametrize(
    ("replacement", "mode"),
    [
        (False, "transient"),
        (True, "transient"),
        (False, "cleanup-denied"),
        (True, "cleanup-denied"),
        (True, "restore-denied"),
        (False, "download-denied"),
        (True, "download-denied"),
    ],
)
def test_native_activation_failure_reports_original_and_recovery_errors(
    scenario: _Scenario,
    replacement: bool,
    mode: str,
    record_property: Callable[[str, object], None],
) -> None:
    """Real native handle failures are observed at the actual activation boundary."""
    context = probe_native_context(
        scenario.isolated.root / "native-context", os.environ["APM_WINDOWS_NATIVE_EXPECTED_SID"]
    )
    record_property("native_context", json.dumps(context, sort_keys=True))
    source = _publish_staging(scenario)
    consumer = scenario.consumers.create("consumer", dependencies=(source.dependency,))
    live = _live(consumer)
    old_lock = None
    prior = None
    sha = source.commit.sha
    if replacement:
        _success(_run(scenario, consumer, source.environment))
        prior = (live / "apm.yml").read_bytes()
        old_lock = (consumer.root / "apm.lock.yaml").read_bytes()
        sha = _advance(scenario, source, consumer)
    manifest = consumer.manifest_path.read_bytes()
    evidence = scenario.isolated.root / "operations.jsonl"
    probe = Path(__file__).parents[1] / "utils" / "resolution_failure_probe.py"
    guard = scenario.isolated.root / "network_guard" / "sitecustomize.py"
    original_guard = guard.read_text(encoding="utf-8")
    guard.write_text(
        original_guard
        + "\nimport runpy\n"
        + f"runpy.run_path({str(probe)!r})['install_probe']("
        + f"{str(live)!r}, {str(evidence)!r}, {mode!r})\n",
        encoding="utf-8",
    )
    try:
        result = _run(scenario, consumer, {**source.environment, "APM_NO_CACHE": "1"})
    finally:
        guard.write_text(original_guard, encoding="utf-8")
    assert evidence.exists(), result.stdout + result.stderr
    events = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()]
    record_property("native_operations", json.dumps(events, sort_keys=True))
    held_paths = {event["path"] for event in events if event["operation"].startswith("held-")}
    failures = [event for event in events if "winerror" in event and event["path"] in held_paths]
    assert failures and failures[0]["winerror"] in {5, 32}, events
    assert all(event["winerror"] in {5, 32, 145} for event in failures), events
    assert result.returncode == 1, result.stdout + result.stderr
    assert consumer.manifest_path.read_bytes() == manifest
    lock = consumer.root / "apm.lock.yaml"
    assert (lock.read_bytes() if lock.exists() else None) == old_lock
    if prior is not None:
        if mode == "restore-denied":
            backups = list(
                (live.parents[1] / ".apm-resolution-staging").glob("*/backups/*/apm.yml")
            )
            assert len(backups) == 1
            assert backups[0].read_bytes() == prior
        else:
            assert (live / "apm.yml").read_bytes() == prior
    output = " ".join((result.stdout + result.stderr).split())
    if mode == "download-denied":
        assert any(event["operation"] == "held-download-index" for event in events)
        checkout_failure = next(
            event for event in events if event["operation"] == "checkout-failed"
        )
        assert checkout_failure["returncode"] != 0
        assert " ".join(checkout_failure["error"].split()) in output, output
        assert not any(event["operation"] == "held-candidate" for event in events)
        assert "Could not discard downloaded candidate" in output, output
    else:
        assert "Failed to activate downloaded dependency" in output, output
    assert failures[0]["error"] in output, output
    if mode != "transient":
        assert len(failures) >= 2, events
        if mode == "restore-denied":
            recovery_failure = next(
                event
                for event in failures
                if event["operation"] == "replace" and "backups" in Path(event["path"]).parts
            )
        else:
            recovery_failure = next(
                event
                for event in failures
                if event["operation"] in {"unlink", "rmdir"} and event["winerror"] in {5, 32}
            )
        assert recovery_failure["error"] in output, output
        assert "recover" in output.lower(), output
    _success(_run(scenario, consumer, source.environment))
    _assert_pin(consumer, sha)
    if prior is not None and mode == "restore-denied":
        assert backups[0].read_bytes() == prior
