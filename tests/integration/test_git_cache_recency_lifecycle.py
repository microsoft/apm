"""Required APMLifecycle transitions for frozen replay and cache pruning."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.locking import shard_lock
from apm_cli.cache.paths import get_git_checkouts_path
from apm_cli.cache.url_normalize import cache_shard_key
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _audit,
    _new_scenario,
    _publish,
    _run_success,
    _skill,
)
from tests.utils.lifecycle_state import LifecycleStateSnapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


@pytest.mark.parametrize(
    "retain_lock_files",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                reason="Retaining real lock files via directory permissions requires non-root POSIX",
            ),
        ),
    ],
    ids=["native-locks", "retained-locks"],
)
def test_frozen_rehydrate_then_prune_preserves_recent_checkout_and_ownership(
    tmp_path: Path, apm_binary_path: Path, retain_lock_files: bool
) -> None:
    """Install -> age -> frozen rehydrate -> prune preserves the used revision."""
    scenario = _new_scenario(tmp_path / "cache-recency", apm_binary_path)
    source = _publish(scenario, "recency-kit", skill="recency-skill")
    consumer = scenario.consumers.create(
        "recency-consumer", dependencies=(source.dependency,), targets=("copilot",)
    )
    _run_success(
        scenario,
        consumer,
        _INSTALL_ARGS,
        environment=source.environment,
        scenario_id="recency-install",
    )
    initial = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert initial.deployment_records
    deployed = ".agents/skills/recency-skill/SKILL.md"
    assert initial.file(deployed).content == _skill("recency-skill").encode("ascii")

    sha_root = (
        get_git_checkouts_path(scenario.isolated.cache_root)
        / cache_shard_key(source.remote_url)
        / source.commit.sha
    )
    variants = tuple(path for path in sha_root.iterdir() if path.is_dir())
    assert variants
    identities = {path: path.stat().st_ino for path in variants}
    (source.repository.worktree / "unused.txt").write_text("stale revision", encoding="ascii")
    stale_commit = scenario.repositories.commit(source.repository, message="unused revision")
    stale = GitCache(scenario.isolated.cache_root).get_checkout(
        source.remote_url, None, locked_sha=stale_commit.sha, env=source.environment
    )
    stale_ns = 946684800000000000
    lock_paths = tuple(Path(shard_lock(variant).lock_file) for variant in variants)
    if retain_lock_files:
        for lock_path in lock_paths:
            lock_path.touch()
    for root in (sha_root, stale.parent):
        os.utime(root, ns=(stale_ns, stale_ns))

    shutil.rmtree(consumer.root / "apm_modules")
    assert (consumer.root / "apm.lock.yaml").read_bytes() == initial.lockfile_bytes
    # Keep existing lock files writable but prevent their incidental unlink
    # from refreshing the SHA root on Unix. Its owner can still set mtime.
    # Windows may retain lock files when another open handle blocks deletion.
    original_mode = sha_root.stat().st_mode
    if retain_lock_files:
        sha_root.chmod(0o500)
    try:
        _run_success(
            scenario,
            consumer,
            (*_INSTALL_ARGS, "--frozen"),
            environment=source.environment,
            scenario_id="recency-frozen-rehydrate",
        )
    finally:
        if retain_lock_files:
            sha_root.chmod(original_mode)
    if retain_lock_files:
        assert all(lock_path.is_file() for lock_path in lock_paths)
    assert {path: path.stat().st_ino for path in variants} == identities
    rehydrated = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert rehydrated.semantic_bytes == initial.semantic_bytes
    assert rehydrated.deployment_records == initial.deployment_records

    result = _run_success(
        scenario,
        consumer,
        ("cache", "prune", "--days", "30"),
        environment=source.environment,
        scenario_id="recency-prune",
    )
    assert sha_root.is_dir(), result.stdout
    assert not stale.parent.exists()
    assert {path: path.stat().st_ino for path in variants} == identities
    pruned = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert pruned.semantic_bytes == rehydrated.semantic_bytes
    assert pruned.deployment_records == initial.deployment_records
    assert pruned.file(deployed).content == initial.file(deployed).content
    _audit(scenario, consumer, environment=source.environment, scenario_id="recency-audit")
