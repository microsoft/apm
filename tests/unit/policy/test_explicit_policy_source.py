"""Explicit ``--policy`` source composition for install/update (#1925)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy import discovery
from apm_cli.policy.discovery import PolicyFetchResult, discover_policy_with_chain
from apm_cli.policy.explicit_source import (
    active_explicit_policy_source,
    compose_explicit_policy,
    explicit_policy_scope,
)
from apm_cli.policy.matcher import check_dependency_allowed
from apm_cli.policy.outcome_routing import (
    classify_discovery_outcome,
    resolve_trust_policy,
    route_discovery_outcome,
)
from apm_cli.policy.parser import load_policy

pytestmark = [pytest.mark.unit, pytest.mark.promise("req-pl-001")]

_DENY_B = "name: org\nversion: '1'\nenforcement: block\ndependencies:\n  deny: [contoso/pkg-b]\n"
_LOOSE = (
    "name: loose\nversion: '1'\nenforcement: 'off'\nfetch_failure: warn\n"
    "dependencies:\n  allow: ['contoso/*']\n"
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with no git remote: auto-discovery finds nothing."""
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    return tmp_path


def _found(text: str, source: str) -> PolicyFetchResult:
    policy, _ = load_policy(text)
    return PolicyFetchResult(policy=policy, source=source, outcome="found")


def test_scope_normalizes_and_restores(project: Path) -> None:
    (project / "p.yml").write_text(_DENY_B, encoding="utf-8")
    with explicit_policy_scope("org") as bound:
        assert bound is None
    with explicit_policy_scope("./p.yml") as bound:
        assert bound == str((project / "p.yml").resolve())
        assert active_explicit_policy_source() == bound
    assert active_explicit_policy_source() is None


def test_explicit_local_file_applies_without_remote(project: Path) -> None:
    (project / "org-policy.yml").write_text(_DENY_B, encoding="utf-8")
    assert discover_policy_with_chain(project).policy is None

    with explicit_policy_scope("./org-policy.yml"):
        result = discover_policy_with_chain(project)

    assert result.explicit and result.outcome == "found"
    assert result.source.endswith("org-policy.yml")
    allowed, _ = check_dependency_allowed("contoso/pkg-b", result.policy.dependencies)
    assert not allowed


def test_explicit_source_cannot_loosen_discovered(project: Path) -> None:
    (project / "loose.yml").write_text(_LOOSE, encoding="utf-8")
    org = _found(_DENY_B, "org:contoso/.github")

    with explicit_policy_scope("./loose.yml"):
        result = compose_explicit_policy(
            org, lambda source: discovery.discover_policy(project, policy_override=source)
        )

    assert result.source.startswith("org:contoso/.github + file:")
    assert result.policy.enforcement == "block"
    allowed, _ = check_dependency_allowed("contoso/pkg-b", result.policy.dependencies)
    assert not allowed


@pytest.mark.parametrize("default", ["warn", "block"])
def test_unavailable_explicit_source_fails_closed_whatever_the_default(
    project: Path, default: str
) -> None:
    result = _partial()
    unreachable_source = "url:https://policy.invalid/p.yml"
    assert [u.source for u in result.unavailable_sources] == [unreachable_source]

    logger = MagicMock()
    with pytest.raises(PolicyViolationError, match="explicit policy source url:https"):
        route_discovery_outcome(result, logger=logger, fetch_failure_default=default)
    logger.policy_source_unavailable.assert_called_once_with(
        source=unreachable_source,
        error="connection refused",
        blocking=True,
        explicit=True,
    )


@pytest.mark.parametrize("default", ["warn", "block"])
def test_discovered_failure_with_loaded_explicit_follows_the_default(
    project: Path, default: str
) -> None:
    """Only the named source fails closed; a failed discovery keeps its routing."""
    (project / "org-policy.yml").write_text(_DENY_B, encoding="utf-8")
    discovered = PolicyFetchResult(
        source="org:contoso/.github", outcome="cache_miss_fetch_fail", error="timed out"
    )
    with explicit_policy_scope("./org-policy.yml"):
        result = compose_explicit_policy(
            discovered, lambda source: discovery.discover_policy(project, policy_override=source)
        )

    logger = MagicMock()
    if default == "block":
        with pytest.raises(PolicyViolationError, match="discovered policy source org:contoso"):
            route_discovery_outcome(result, logger=logger, fetch_failure_default=default)
    else:
        policy = route_discovery_outcome(result, logger=logger, fetch_failure_default=default)
        assert policy is not None and policy.dependencies.deny == ("contoso/pkg-b",)
    logger.policy_source_unavailable.assert_called_once_with(
        source="org:contoso/.github", error="timed out", blocking=default == "block"
    )


def test_stale_cached_explicit_url_counts_as_loaded(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid stale cached copy of the named source is loaded, not a failure."""
    url = "https://policy.example.com/org-policy.yml"
    online = {"up": True}

    def transport(_session, method, url=None, **_kwargs):
        if not online["up"]:
            raise requests.ConnectionError("connection refused")
        response = requests.Response()
        response.status_code = 200
        response._content = _DENY_B.encode("utf-8")
        response.encoding = "utf-8"
        response.url = url
        return response

    monkeypatch.setattr(requests.Session, "request", transport)
    with explicit_policy_scope(url):
        fresh = discover_policy_with_chain(project)
    assert fresh.outcome == "found"

    meta = next(discovery._get_cache_dir(project).glob("*.meta.json"))
    payload = json.loads(meta.read_text(encoding="utf-8"))
    payload["cached_at"] = time.time() - discovery.DEFAULT_CACHE_TTL - 60
    meta.write_text(json.dumps(payload), encoding="utf-8")
    online["up"] = False

    with explicit_policy_scope(url):
        stale = discover_policy_with_chain(project)

    assert stale.outcome == "cached_stale"
    assert stale.unavailable_sources == []
    policy = route_discovery_outcome(stale, logger=None, fetch_failure_default="block")
    allowed, _ = check_dependency_allowed("contoso/pkg-b", policy.dependencies)
    assert not allowed


def test_missing_explicit_file_is_a_routed_failure(project: Path) -> None:
    with explicit_policy_scope("./missing.yml"):
        result = discover_policy_with_chain(project)
    assert result.policy is None
    [failed] = result.unavailable_sources
    assert failed.outcome == "cache_miss_fetch_fail"
    assert "not found" in failed.error


def test_unavailable_explicit_url_is_redacted(project: Path) -> None:
    secret = "https://user:s3cret@policy.invalid/p.yml?sig=TOKEN"
    with explicit_policy_scope(secret):
        result = discover_policy_with_chain(project, cache_only=True)
    [failed] = result.unavailable_sources
    assert "s3cret" not in failed.source and "TOKEN" not in failed.source
    assert failed.error == "no cached copy available offline"


def _partial() -> PolicyFetchResult:
    """Discovered org policy composed with an explicit source that did not load."""
    org = _found(_DENY_B, "org:contoso/.github")
    unreachable = PolicyFetchResult(
        source="url:https://policy.invalid/p.yml",
        outcome="cache_miss_fetch_fail",
        error="connection refused",
    )
    with explicit_policy_scope("https://policy.invalid/p.yml"):
        return compose_explicit_policy(org, lambda _source: unreachable)


@pytest.mark.parametrize("default", ["warn", "block"])
def test_partial_policy_is_unresolved_and_blocks_for_every_reader(default: str) -> None:
    route = classify_discovery_outcome(_partial(), fetch_failure_default=default)
    assert route.unresolved
    assert route.unavailable_source == "cache_miss_fetch_fail"
    assert route.blocks
    assert not route.from_fetch_failure_default


def test_trust_reader_fails_closed_on_an_unavailable_explicit_source(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trust reader whose logger has no policy wording still names the source."""
    result = _partial()

    class _PlainLogger:
        verbose = False

    with pytest.raises(PolicyViolationError, match="explicit policy source"):
        resolve_trust_policy(project, logger=_PlainLogger(), fetch_result=result)

    captured = capsys.readouterr()
    assert (
        "Explicit policy source url:https://policy.invalid/p.yml could not be loaded"
        in captured.out + captured.err
    )


def test_install_logger_names_an_explicit_source_once_even_when_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from apm_cli.core.command_logger import InstallLogger

    logger = InstallLogger(verbose=False)
    logger.policy_resolved(source="file:/p.yml", cached=False, enforcement="off", explicit=True)
    for _ in range(2):
        logger.policy_source_unavailable(source="org:contoso/.github", error="timed out")

    out = capsys.readouterr()
    text = out.out + out.err
    assert "Policy: file:/p.yml" in text
    assert text.count("Discovered policy source org:contoso/.github could not be loaded") == 1
