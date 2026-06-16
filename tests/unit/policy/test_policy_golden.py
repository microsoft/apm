"""Seam 1: golden parse-equivalence over every policy fixture.

Loads EVERY top-level fixture under ``tests/fixtures/policy/``, parses each to
an :class:`ApmPolicy`, canonicalizes it to a stable JSON form, and asserts it
equals a checked-in golden snapshot.

Why this proves the additive / non-breaking claim: the golden snapshot is
regenerated only when the schema legitimately changes. Adding the two
default-off integrity keys (``security.integrity.require_hashes`` and
``security.audit.fail_on_drift``) appears in the golden diff as exactly those
two additive fields and nothing else -- any unexpected change to how an
existing field parses would surface as an extra diff and fail review.

``APM_REGEN_POLICY_GOLDEN=1 pytest ...`` regenerates the snapshot; the test
also bootstraps the snapshot on first run if it is missing.
"""

from __future__ import annotations

import dataclasses
import json
import os
import unittest
from pathlib import Path

from apm_cli.policy.parser import load_policy

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "policy"
GOLDEN_PATH = FIXTURES_DIR / "golden" / "parsed-policies.json"


def _canonicalize(policy) -> dict:
    """Recursive, JSON-stable canonical form of a parsed ApmPolicy.

    Round-trips through JSON so tuples and lists compare equal -- the snapshot
    and the checked-in golden are both normalized to JSON primitives.
    """
    return json.loads(json.dumps(dataclasses.asdict(policy), sort_keys=True))


def _build_snapshot() -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    for yml in sorted(FIXTURES_DIR.glob("*.yml")):
        policy, _ = load_policy(yml)
        snapshot[yml.name] = _canonicalize(policy)
    return snapshot


class TestPolicyGoldenParseEquivalence(unittest.TestCase):
    def test_fixtures_match_golden(self):
        snapshot = _build_snapshot()

        regen = os.environ.get("APM_REGEN_POLICY_GOLDEN") == "1"
        if regen or not GOLDEN_PATH.exists():
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        # Exactness guard: the fixture set on disk and the golden set must
        # match 1:1. Without this, adding a new *.yml fixture WITHOUT
        # regenerating the snapshot would still pass -- the new fixture is
        # simply absent from `golden` and never asserted, silently skipping
        # coverage. Compare the key sets directly.
        self.assertEqual(
            set(snapshot.keys()),
            set(golden.keys()),
            "fixture set and golden snapshot set diverged; regenerate with "
            "APM_REGEN_POLICY_GOLDEN=1 after adding or removing a policy fixture",
        )

        # Every previously-snapshotted fixture must parse identically.
        for name, canonical in golden.items():
            with self.subTest(fixture=name):
                self.assertIn(name, snapshot, f"fixture {name} disappeared")
                self.assertEqual(
                    snapshot[name],
                    canonical,
                    f"parse output for {name} drifted from golden snapshot",
                )

    def test_silent_fixtures_get_default_off_keys(self):
        """Fixtures that never mention the new keys parse them as default-off."""
        for yml in sorted(FIXTURES_DIR.glob("*.yml")):
            raw = yml.read_text(encoding="utf-8")
            if "require_hashes" in raw or "fail_on_drift" in raw:
                continue
            policy, _ = load_policy(yml)
            with self.subTest(fixture=yml.name):
                self.assertFalse(policy.security.integrity.require_hashes)
                self.assertFalse(policy.security.audit.fail_on_drift)


if __name__ == "__main__":
    unittest.main()
