"""Generated obligation space of the lifecycle security tables and journeys.

The security space comes from the reviewed model export
(``tests/quality/security_obligations.json``), never from the tables that
cover it; journey obligations are the step oracles of the journey index.
``oracles.check_obligation`` is the only way to credit one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.lifecycle import obligation_space, oracles, receipts
from tests.lifecycle.obligations import (
    GENERATED_PATH,
    Obligation,
    load_catalog,
    load_generated,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise(
        "P-INV-I1", "P-INV-I4", "P-INV-I5", "P-INV-I7", "P-LAW-auth.credentials_isolated"
    ),
]


def test_security_space_matches_the_reviewed_counts() -> None:
    export = obligation_space.load_export()
    space = obligation_space.security_obligations(export)
    counts = {key: len(ids) for key, ids in space.items()}
    assert counts == obligation_space.EXPECTED_COUNTS == export["counts"]
    assert counts == {"G": 244, "P": 224, "I": 116, "E": 281, "M": 38, "C": 22}
    every = [key for ids in space.values() for key in ids]
    assert len(every) == len(set(every)) == 925


def test_security_ids_are_ascii_and_carry_their_space() -> None:
    for space, ids in obligation_space.security_obligations().items():
        for key, strength in ids.items():
            assert key.isascii(), key
            assert key.split("|", 1)[0] in {space, "E1", "E2", "EP"}, key
            assert strength in {"unit", "e2e", "e2e-evidence"}, key


def test_policy_collapse_stays_off_until_the_routing_owner_lands() -> None:
    assert obligation_space.P_COLLAPSE_APPROVED_AFTER_H1 is False


def test_journey_space_is_one_row_per_index_step_oracle() -> None:
    rows = obligation_space.journey_obligations()
    if not obligation_space.JOURNEY_INDEX_PATH.is_file():
        assert rows == []
        return
    assert len(rows) == len(set(rows))
    assert all(re.fullmatch(r"J\|[JPE]\d\d\|\d+\|(O\d+|I\d)", row) for row in rows), rows


def test_generated_file_names_only_known_ids_and_real_witnesses() -> None:
    universe, required, baseline, known = load_generated()
    data = json.loads(GENERATED_PATH.read_text(encoding="ascii"))
    assert set(data) == {"schema_version", "description", "required", "known_failing"}
    assert list(required) == sorted(required) and list(known) == sorted(known)
    assert set(required) <= set(universe), sorted(set(required) - set(universe))
    assert set(known) <= set(universe), sorted(set(known) - set(universe))
    assert not set(required) & set(known)
    assert set(required) | set(known) | set(baseline) == set(universe)
    for key, tracking in known.items():
        assert re.fullmatch(r"#\d+", tracking), (key, tracking)
    root = Path(__file__).parents[2]
    for key, witness in required.items():
        path, _, name = witness.partition("::")
        assert (root / path).is_file(), (key, witness)
        assert re.search(rf"def {re.escape(name.split('[')[0])}\b", (root / path).read_text()), key


def test_catalog_witness_index_includes_generated_rows() -> None:
    catalog = load_catalog()
    for key, witness in catalog.generated_required.items():
        assert catalog.generated[key] in catalog.witnessed_by(witness)


def _result(rc: int = 0, out: str = "") -> CommandResult:
    return CommandResult(
        command=("apm", "install"), returncode=rc, stdout=out, stderr="", cwd=Path()
    )


def _pair(tmp_path: Path, before_files: dict[str, str], after_files: dict[str, str]):
    root = tmp_path / "project"
    root.mkdir()
    for name, text in before_files.items():
        (root / name).write_text(text, encoding="ascii")
    before = ArtifactSnapshotSet.capture({"project": root})
    for name in before_files:
        (root / name).unlink()
    for name, text in after_files.items():
        (root / name).write_text(text, encoding="ascii")
    return before, ArtifactSnapshotSet.capture({"project": root})


def test_check_obligation_credits_only_verified_evidence(tmp_path: Path) -> None:
    before, after = _pair(tmp_path, {"hook.json": "x", "keep.md": "k"}, {"keep.md": "k"})
    evidence = oracles.Evidence(
        absent={"project": ["hook.json"]}, unchanged={"project": ["keep.md"]}
    )
    start = len(receipts.earned())
    oracles.check_obligation(
        "G|install|local-deny|hook",
        strength="e2e-evidence",
        before=before,
        after=after,
        result=_result(),
        evidence=evidence,
    )
    receipt = receipts.earned()[start]
    assert receipt.oracle_id == oracles.OBLIGATION_ORACLE
    assert Obligation("G|install|local-deny|hook", strength="e2e-evidence").covered_by(receipt)
    assert not Obligation("G|install|local-deny|mcp", strength="e2e-evidence").covered_by(receipt)


@pytest.mark.parametrize(
    ("strength", "evidence", "message"),
    [
        ("e2e-evidence", oracles.Evidence(exit=0), "needs a snapshot"),
        ("unit", oracles.Evidence(exit=0), "expected/observed"),
        ("e2e", oracles.Evidence(absent={"project": ["hook.json"]}), "must be absent"),
        ("unit", oracles.Evidence(expected="block", observed="warn"), "observed 'warn'"),
        ("e2e-evidence", oracles.Evidence(secrets_absent=("tok-123",)), "carries secret"),
    ],
)
def test_check_obligation_refuses_missing_or_false_evidence(
    tmp_path: Path, strength: str, evidence: oracles.Evidence, message: str
) -> None:
    before, after = _pair(tmp_path, {"hook.json": "x"}, {"hook.json": "tok-123"})
    start = len(receipts.earned())
    with pytest.raises(AssertionError, match=message):
        oracles.check_obligation(
            "G|install|local-deny|hook",
            strength=strength,
            before=before,
            after=after,
            result=_result(out="tok-123" if "secret" in message else ""),
            evidence=evidence,
        )
    assert len(receipts.earned()) == start


def test_receipts_for_generated_ids_cannot_be_forged() -> None:
    forged = receipts.Receipt(
        node="x",
        params={"obligation": "C|lock|github-PAT", "strength": "e2e-evidence"},
        pre_digest="",
        argv=(),
        scope="project",
        post_digest="",
        oracle_id="OBLIGATION",
        outcome="success",
    )
    with pytest.raises(receipts.ReceiptForgeryError):
        receipts.emit(forged)


# -- real negative control: remove the oracle call path from a real witness ----

_DROP_CALL_PLUGIN = """\
import tests.lifecycle.oracles as oracles


def pytest_configure(config):
    oracles.check_obligation = lambda *args, **kwargs: None
"""


def _run_witness(node: str, tmp_path: Path, *plugins: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(root)]),
        receipts.DIRECTORY_ENV: str(tmp_path / "receipts"),
    }
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop(receipts.SESSION_ENV, None)
    extra = [item for plugin in plugins for item in ("-p", plugin)]
    return subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "-n", "0", "-p", "no:cacheprovider", *extra],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_a_real_witness_without_its_oracle_call_fails_the_ratchet(tmp_path: Path) -> None:
    """Negative control: the same witness passes with the call and fails without it."""
    catalog = load_catalog()
    owner_rows = sorted(key for key in catalog.generated_required if key.startswith("P|owner|"))
    assert owner_rows, "no P|owner witness is registered"
    witness = catalog.generated_required[owner_rows[0]]
    kept = _run_witness(witness, tmp_path)
    assert kept.returncode == 0, kept.stdout + kept.stderr
    (tmp_path / "drop_obligation_call.py").write_text(_DROP_CALL_PLUGIN, encoding="ascii")
    dropped = _run_witness(witness, tmp_path, "drop_obligation_call")
    assert dropped.returncode != 0, dropped.stdout
    assert "no oracle receipt covered" in dropped.stdout, dropped.stdout
    assert owner_rows[0] in dropped.stdout, dropped.stdout
