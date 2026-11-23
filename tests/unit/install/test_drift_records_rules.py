"""Drift replay record serving: the rules it shares with other owners (#4940)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.drift_records import record_policy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-audit-scratch-materialization")]


@pytest.mark.parametrize(
    ("outcome", "discoveries"),
    [("found", 1), ("absent", 1), ("cache_miss_fetch_fail", 2), ("cached_stale", 2)],
)
def test_the_shared_discovery_keeps_only_answers_a_command_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, discoveries: int
) -> None:
    calls: list[Path] = []

    def discover(root: Path) -> SimpleNamespace:
        calls.append(root)
        return SimpleNamespace(outcome=outcome)

    # The discovery I/O boundary below the trust-policy owner.
    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy_with_chain", discover)
    config = SimpleNamespace(policy_provider=None, policy_fetch=None, user_scope=False)
    fetch, _provider = record_policy(config, tmp_path, enabled=True)

    assert fetch().outcome == outcome
    assert fetch().outcome == outcome
    assert len(calls) == discoveries


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
