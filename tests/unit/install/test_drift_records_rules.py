"""Drift replay record serving: the rules it shares with other owners (#4940)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.drift_records import record_policy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-audit-scratch-materialization")]


def _discoveries(monkeypatch: pytest.MonkeyPatch, outcomes: list[str]) -> list[str]:
    """Fake discovery answers in order; returns the list of answers handed out."""
    handed: list[str] = []
    pending = list(outcomes)

    def discover(_root: Path) -> SimpleNamespace:
        handed.append(pending.pop(0))
        return SimpleNamespace(outcome=handed[-1])

    # The discovery I/O boundary below the trust-policy owner.
    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy_with_chain", discover)
    return handed


def _gate_answers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Outcomes the executables gate resolves its policy from, in order."""
    seen: list[str] = []

    def resolve(_root: Path, *, announce: bool, fetch_result: SimpleNamespace) -> None:
        seen.append(fetch_result.outcome)

    monkeypatch.setattr("apm_cli.policy.outcome_routing.resolve_trust_policy", resolve)
    return seen


@pytest.mark.parametrize(
    ("outcomes", "gate", "discovered"),
    [
        (["found", "absent"], ["found", "found"], 1),
        (["absent", "found"], ["absent", "absent"], 1),
        # A live replay's gate resolves its first discovery; a retry may differ.
        (["cache_miss_fetch_fail", "found"], ["cache_miss_fetch_fail", "found"], 2),
        (["cached_stale", "found"], ["cached_stale", "found"], 2),
    ],
)
def test_the_gate_first_evaluates_the_discovery_a_live_replay_would(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[str],
    gate: list[str],
    discovered: int,
) -> None:
    handed = _discoveries(monkeypatch, outcomes)
    seen = _gate_answers(monkeypatch)
    config = SimpleNamespace(policy_provider=None, policy_fetch=None, user_scope=False)
    fetch, provider = record_policy(config, tmp_path, enabled=True)

    assert fetch().outcome == outcomes[0]  # the records' probe
    provider()
    provider()

    assert seen == gate
    assert len(handed) == discovered


def test_the_gate_evaluating_first_discovers_as_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handed = _discoveries(monkeypatch, ["cache_miss_fetch_fail", "found", "found"])
    seen = _gate_answers(monkeypatch)
    config = SimpleNamespace(policy_provider=None, policy_fetch=None, user_scope=False)
    fetch, provider = record_policy(config, tmp_path, enabled=True)

    provider()
    assert fetch().outcome == "found"  # the probe discovers after the gate
    provider()

    assert seen == ["cache_miss_fetch_fail", "found"]
    assert handed == ["cache_miss_fetch_fail", "found"]


@pytest.mark.parametrize(
    ("hashed", "directory"),
    [
        ([".github/skills/a/SKILL.md"], ".github/skills/a"),
        ([".github/skills/a/SKILL.md"], ".github/skills/a/"),
        ([".github/skills/a/SKILL.md"], ".github/skills"),
        ([".github/skills/a/SKILL.md"], ".github/skills/ab"),
        ([".github/skills/ab/SKILL.md"], ".github/skills/a"),
        ([".github/skills/a/b/c.md"], ".github/skills/a/b"),
        ([".github/skills/a/SKILL.md"], ".github/skills/a/SKILL.md"),
        ([".github/x.md"], ""),
        ([], ".github"),
    ],
)
def test_the_directory_binding_index_matches_the_prefix_scan(
    hashed: list[str], directory: str
) -> None:
    from apm_cli.install.drift_records import _recorded_parents

    prefix = directory.rstrip("/") + "/"

    assert (directory.rstrip("/") in _recorded_parents(hashed)) is any(
        rel.startswith(prefix) for rel in hashed
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/a",
        "http://h",
        "https://",
        "http:rel/path",
        " https://h/x ",
        "ftp://h/f",
        "javascript:alert",
        "../a.md",
        "/abs/b.context.md",
        "#anchor",
    ],
)
def test_the_live_link_rule_is_the_resolver_rule(tmp_path: Path, target: str) -> None:
    from apm_cli.compilation.link_resolver import UnifiedLinkResolver
    from apm_cli.install.drift_records import _may_rewrite_links

    resolver = UnifiedLinkResolver(tmp_path)
    data = f"[x]({target})".encode()

    assert _may_rewrite_links(data) is not resolver._is_external_url(target)
