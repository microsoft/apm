"""The effective-policy fingerprint install records and audit compares (#4940)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.policy.outcome_routing import effective_policy_fingerprint
from apm_cli.policy.parser import load_policy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-audit-scratch-materialization")]


def _fetch(outcome: str, policy_text: str | None = None, **fields: object) -> SimpleNamespace:
    policy = load_policy(policy_text)[0] if policy_text is not None else None
    base = {"outcome": outcome, "policy": policy, "cache_stale": False, "fetch_error": None}
    return SimpleNamespace(**{**base, "error": None, **fields})


_DENY_A = "name: org\nversion: '1.0'\ndependencies:\n  deny:\n    - acme/a\n"
_DENY_B = "name: org\nversion: '1.0'\ndependencies:\n  deny:\n    - acme/b\n"


def test_each_known_outcome_and_policy_fingerprints_distinctly() -> None:
    prints = [
        effective_policy_fingerprint(_fetch("found", _DENY_A)),
        effective_policy_fingerprint(_fetch("found", _DENY_B)),
        effective_policy_fingerprint(_fetch("absent")),
        effective_policy_fingerprint(_fetch("no_git_remote", error="no remote")),
        effective_policy_fingerprint(_fetch("disabled")),
        effective_policy_fingerprint(None, disabled=True),
    ]

    assert all(isinstance(value, str) and len(value) == 64 for value in prints)
    assert len(set(prints)) == len(prints)
    assert prints[0] == effective_policy_fingerprint(_fetch("found", _DENY_A))


@pytest.mark.parametrize(
    "fetch",
    [
        None,
        _fetch("cached_stale", _DENY_A),
        _fetch("found", _DENY_A, cache_stale=True),
        _fetch("found", _DENY_A, fetch_error="timeout"),
        _fetch("found", _DENY_A, error="partial"),
        _fetch("found"),
        _fetch("cache_miss_fetch_fail"),
        _fetch("hash_mismatch", _DENY_A),
        _fetch("incomplete_chain", _DENY_A),
        _fetch("malformed"),
        _fetch("found", _DENY_A, unavailable_sources=[_fetch("cache_miss_fetch_fail")]),
    ],
)
def test_an_uncertain_effective_policy_has_no_fingerprint(fetch: object) -> None:
    assert effective_policy_fingerprint(fetch) is None
