"""M-SPINE: generated install, audit and repair sequences over the real CLI (#4098).

The driver runs every command (``python -m apm_cli.cli`` on PR, the packaged
binary on the nightly) and ``check_cross_cutting`` checks I1-I9 after each
one. The mandatory spine (#2867 witness) replays once per world; generated
examples restore that post-spine world (``max_examples`` 4 on PR,
``APM_SPINE_MAX_EXAMPLES`` raises it for the nightly shard).
"""

from __future__ import annotations

import ast
import inspect
import itertools
import os
import textwrap
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis.stateful import run_state_machine_as_test

from apm_cli.deps.lockfile import LockFile
from tests.lifecycle.machines._spine_kit import (
    _PHASE_ONE_PROPERTIES,
    _TRANSITION_PROPERTIES,
    _VARIANTS,
    USER_EDIT_CELL,
    _LifecycleReferenceModel,
    _ModelFixture,
    _SpineWorld,
    mandatory_replay,
)
from tests.utils.artifact_snapshot import assert_snapshot_changes_within

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I4",
        "P-INV-I5",
        "P-INV-I6",
        "P-INV-I7",
        "P-INV-I8",
        "P-INV-I9",
        "P-LAW-filesystem.open_world_observation",
        "P-LAW-idempotency.byte_stable",
        "P-LAW-lockfile.frozen_preserves_bytes",
        "P-LAW-outcome.status_matches_state",
        "P-LAW-ownership.preserve_unowned",
        "P-LAW-routing.authorized_targets_only",
        "P-LAW-source.ref_cache_coherent",
        "P-LAW-transaction.failed_command_preserves_state",
    ),
]


@pytest.mark.parametrize("variant", _VARIANTS)
def test_generated_lifecycle_sequences_preserve_reference_model(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], variant: str
) -> None:
    """Generate and shrink guarded transition sequences over a real CLI.

    The mandatory spine replays once per world, inside the first generated
    model; every later example restores that post-spine world instead of
    replaying the spine again.
    """
    world: _SpineWorld | None = None

    def factory() -> _LifecycleReferenceModel:
        nonlocal world
        if world is None:
            world, model = _SpineWorld.replay(tmp_path / "world", lifecycle_entry_point, variant)
            return model
        return world.restore()

    run_state_machine_as_test(
        factory,
        settings=settings(
            database=None,
            deadline=None,
            # PR runs are reproducible; the nightly explores fresh examples and
            # print_blob reports a reproducer for any failure.
            derandomize="APM_SPINE_MAX_EXAMPLES" not in os.environ,
            max_examples=int(os.environ.get("APM_SPINE_MAX_EXAMPLES", "4")),
            phases=(Phase.generate, Phase.shrink),
            print_blob=True,
            stateful_step_count=8,
            suppress_health_check=(HealthCheck.too_slow,),
        ),
    )


@pytest.mark.parametrize("variant", _VARIANTS)
def test_generated_lifecycle_mandatory_replay(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], variant: str
) -> None:
    """Exercise the model's guarded commands without relying on random selection."""
    model = _LifecycleReferenceModel(
        _ModelFixture.create(tmp_path / "mandatory", lifecycle_entry_point, variant)
    )
    mandatory_replay(model)
    # The spine tampers a managed file, so the preserved-edit cell must still occur.
    assert model.expected_seen == {USER_EDIT_CELL}


@pytest.mark.parametrize(
    ("extra_write", "variant"),
    [
        *itertools.product(("identity-sibling", "wrong-root"), _VARIANTS),
        ("receipt-sibling", "project"),
    ],
)
def test_generated_identity_write_scope_is_exact(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...], variant: str, extra_write: str
) -> None:
    """Admit local evidence, but reject sibling files and cross-scope writes."""
    model = _LifecycleReferenceModel(
        _ModelFixture.create(tmp_path / "identity-scope", lifecycle_entry_point, variant)
    )
    model.install()
    home = model.scenario.isolated.home
    project = model.fixture.invoke_cwd
    identity_root = home if model.fixture.global_scope else project
    identity = identity_root / ".apm-local/installation"
    assert identity.is_file() and not identity.is_symlink()
    identity_bytes = identity.read_bytes()
    assert identity_bytes == (UUID(hex=identity_bytes.decode("ascii").strip()).hex + "\n").encode(
        "ascii"
    )
    ignore = identity.parent / ".gitignore"
    assert ignore.is_file() and not ignore.is_symlink()
    assert ignore.read_bytes() == b"*\n"

    before = model._capture()
    if extra_write == "identity-sibling":
        unexpected = identity.parent / "unrelated.txt"
    elif extra_write == "receipt-sibling":
        unexpected = home / ".apm/deployment-receipts/unrelated.json"
    else:
        wrong_root = project if model.fixture.global_scope else home
        unexpected = wrong_root / ".apm-local/installation"
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_bytes(identity_bytes)
    with pytest.raises(AssertionError, match="outside its declared write set"):
        assert_snapshot_changes_within(
            before,
            model._capture(),
            exact_paths=model.fixture.exact_paths,
            tree_prefixes=model.fixture.tree_prefixes,
        )


def test_generated_transition_catalog_covers_phase_one_properties() -> None:
    """Require every generated transition to invoke a filesystem oracle."""
    rules = {
        name: value
        for name, value in vars(_LifecycleReferenceModel).items()
        if getattr(value, "hypothesis_stateful_rule", None) is not None
    }

    assert set(_TRANSITION_PROPERTIES) == set(rules)
    assert set().union(*_TRANSITION_PROPERTIES.values()) == set(_PHASE_ONE_PROPERTIES)
    oracle_calls = {
        "assert_only_snapshot_paths_changed",
        "assert_snapshot_changes_within",
        "assert_snapshot_set_unchanged",
    }
    for rule_name, function in rules.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(function))))
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called_names & oracle_calls, f"{rule_name} does not invoke a filesystem oracle"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="tracking #4248: a --global install records its deployment rows as scope project",
)
def test_global_install_records_user_scope_deployment_rows(
    tmp_path: Path, lifecycle_entry_point: tuple[str, ...]
) -> None:
    """After ``apm install --global`` every user-lock deployment row records scope user."""
    model = _LifecycleReferenceModel(
        _ModelFixture.create(
            tmp_path / "user-scope-rows", lifecycle_entry_point, "global-canonical"
        )
    )
    model.install()
    lock = LockFile.read(model.project.root / "apm.lock.yaml")
    assert lock is not None
    scopes = sorted({record.locator.scope for record in lock.deployment_ledger.records.values()})
    assert scopes == ["user"], scopes
