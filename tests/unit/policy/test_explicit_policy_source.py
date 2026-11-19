"""Explicit ``--policy`` source composition for install/update (#1925)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy import discovery
from apm_cli.policy.discovery import PolicyFetchResult, discover_policy_with_chain
from apm_cli.policy.explicit_source import (
    active_explicit_policy_source,
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


def test_explicit_source_cannot_loosen_discovered(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "loose.yml").write_text(_LOOSE, encoding="utf-8")
    org = _found(_DENY_B, "org:contoso/.github")
    real = discovery.discover_policy
    monkeypatch.setattr(
        discovery,
        "discover_policy",
        lambda root, policy_override=None, **kw: (
            real(root, policy_override=policy_override, **kw) if policy_override else org
        ),
    )

    with explicit_policy_scope("./loose.yml"):
        result = discover_policy_with_chain(project)

    assert result.source.startswith("org:contoso/.github + file:")
    assert result.policy.enforcement == "block"
    allowed, _ = check_dependency_allowed("contoso/pkg-b", result.policy.dependencies)
    assert not allowed


@pytest.mark.parametrize("default", ["warn", "block"])
def test_unavailable_explicit_source_follows_fetch_failure_default(
    project: Path, monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    org = _found(_DENY_B, "org:contoso/.github")
    unreachable = PolicyFetchResult(
        source="url:https://policy.invalid/p.yml",
        outcome="cache_miss_fetch_fail",
        error="connection refused",
    )
    monkeypatch.setattr(
        discovery,
        "discover_policy",
        lambda root, policy_override=None, **kw: unreachable if policy_override else org,
    )
    with explicit_policy_scope("https://policy.invalid/p.yml"):
        result = discover_policy_with_chain(project)
    assert [u.source for u in result.unavailable_sources] == [unreachable.source]

    logger = MagicMock()
    if default == "block":
        with pytest.raises(PolicyViolationError, match="could not be loaded"):
            route_discovery_outcome(result, logger=logger, fetch_failure_default=default)
    else:
        policy = route_discovery_outcome(result, logger=logger, fetch_failure_default=default)
        assert policy is org.policy
    logger.policy_source_unavailable.assert_called_once_with(
        source=unreachable.source,
        error="connection refused",
        blocking=default == "block",
    )


def test_missing_explicit_file_is_a_routed_failure(project: Path) -> None:
    with explicit_policy_scope("./missing.yml"):
        result = discover_policy_with_chain(project)
    assert result.policy is None
    [failed] = result.unavailable_sources
    assert failed.outcome == "cache_miss_fetch_fail"
    assert "not found" in failed.error


@pytest.mark.parametrize(
    ("command", "args", "message"),
    [
        ("install", ["--policy", "p.yml", "--global"], "--global"),
        ("install", ["--policy", "p.yml", "--no-policy"], "mutually exclusive"),
        ("update", ["--policy", "p.yml", "--global"], "--global"),
    ],
)
def test_policy_flag_rejects_undefined_combinations(
    project: Path, command: str, args: list[str], message: str
) -> None:
    from apm_cli.cli import cli

    result = CliRunner().invoke(cli, [command, *args])
    assert result.exit_code == 2, result.output
    assert message in result.output


def test_unavailable_explicit_url_is_redacted(project: Path) -> None:
    secret = "https://user:s3cret@policy.invalid/p.yml?sig=TOKEN"
    with explicit_policy_scope(secret):
        result = discover_policy_with_chain(project, cache_only=True)
    [failed] = result.unavailable_sources
    assert "s3cret" not in failed.source and "TOKEN" not in failed.source
    assert failed.error == "no cached copy available offline"


def _partial(project: Path, monkeypatch: pytest.MonkeyPatch) -> PolicyFetchResult:
    """Discovered org policy composed with an explicit source that did not load."""
    org = _found(_DENY_B, "org:contoso/.github")
    unreachable = PolicyFetchResult(
        source="url:https://policy.invalid/p.yml",
        outcome="cache_miss_fetch_fail",
        error="connection refused",
    )
    monkeypatch.setattr(
        discovery,
        "discover_policy",
        lambda root, policy_override=None, **kw: unreachable if policy_override else org,
    )
    with explicit_policy_scope("https://policy.invalid/p.yml"):
        return discover_policy_with_chain(project)


@pytest.mark.parametrize("default", ["warn", "block"])
def test_partial_policy_is_unresolved_for_every_reader(
    project: Path, monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    route = classify_discovery_outcome(
        _partial(project, monkeypatch), fetch_failure_default=default
    )
    assert route.unresolved
    assert route.unavailable_source == "cache_miss_fetch_fail"
    assert route.blocks is (default == "block")


def test_trust_reader_routes_an_unavailable_explicit_source(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trust reader whose logger has no policy wording reports the source neutrally."""
    result = _partial(project, monkeypatch)

    class _PlainLogger:
        verbose = False

    policy = resolve_trust_policy(project, logger=_PlainLogger(), fetch_result=result)

    assert policy is not None
    assert "url:https://policy.invalid/p.yml could not be loaded" in capsys.readouterr().err
