"""GitLab namespace-ancestry policy composition (#2753).

Drives ``discover_policy_with_chain`` through the real discovery, GitLab
adapter, cache and tighten-only merger; only the GitLab REST transport and
the origin remote are faked. Every discovered ancestor constrains the
nearest (leaf) policy even when the leaf omits ``extends:``.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.policy import discovery
from apm_cli.policy.discovery import discover_policy_with_chain
from apm_cli.policy.inheritance import PolicyInheritanceError

_REMOTE = "https://gitlab.com/acme/dept-a/team-x/service.git"
_ACME = "name: company\nenforcement: block\ndependencies:\n  deny: [evil/pkg]\n"
_DEPT = "name: dept\nenforcement: warn\ndependencies:\n  deny: [dept/pkg]\n"
_TEAM = "name: team\nenforcement: warn\ndependencies:\n  deny: [team/pkg]\n"


class _GitLab:
    """Fake GitLab Repository Files transport keyed by project path."""

    def __init__(self, projects: dict[str, str | int]) -> None:
        self.projects = projects
        self.calls: list[str] = []

    def __call__(self, org, repo, _file, *, host="gitlab.com", port=None):
        path = f"{org}/{repo}"
        self.calls.append(path)
        value = self.projects.get(path, 404)
        if isinstance(value, str):
            return value, None
        if value == 404:
            return None, "gitlab-status:404: Policy file not found"
        return None, f"HTTP {value} fetching policy from {host}/{path}"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.delenv("APM_GITLAB_POLICY_REPO", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    return root


def _discover(project: Path, gitlab: _GitLab, remote: str = _REMOTE, **kwargs):
    with (
        patch.object(discovery, "_git_remote_origin_url", return_value=remote),
        patch("apm_cli.policy._gitlab._fetch_gitlab_contents", side_effect=gitlab),
    ):
        return discover_policy_with_chain(project, **kwargs)


def _deny(result) -> set[str]:
    return set(result.policy.dependencies.deny or ())


def _policy(name: str, *, deny: str, extends: str | None = None) -> str:
    text = f"name: {name}\nenforcement: warn\ndependencies:\n  deny: [{deny}]\n"
    return text if extends is None else f"{text}extends: {extends}\n"


def test_company_only_policy_applies_to_nested_project(project: Path) -> None:
    result = _discover(project, _GitLab({"acme/apm-policy": _ACME}))

    assert result.outcome == "found"
    assert _deny(result) == {"evil/pkg"}
    assert result.chain_sources == ["gitlab.com/acme/apm-policy"]
    assert result.subgroup_scoped is False


def test_team_only_policy_applies_without_ancestor(project: Path) -> None:
    result = _discover(project, _GitLab({"acme/dept-a/team-x/apm-policy": _TEAM}))

    assert result.outcome == "found"
    assert _deny(result) == {"team/pkg"}
    assert result.chain_sources == ["gitlab.com/acme/dept-a/team-x/apm-policy"]
    assert result.subgroup_scoped is True


def test_company_and_team_compose_with_missing_intermediate(project: Path) -> None:
    gitlab = _GitLab({"acme/apm-policy": _ACME, "acme/dept-a/team-x/apm-policy": _TEAM})

    result = _discover(project, gitlab)

    assert _deny(result) == {"evil/pkg", "team/pkg"}
    assert result.policy.enforcement == "block"
    assert result.policy.name == "team"
    assert result.source == "org:gitlab.com/acme/dept-a/team-x/apm-policy"
    assert result.chain_sources == [
        "gitlab.com/acme/apm-policy",
        "gitlab.com/acme/dept-a/team-x/apm-policy",
    ]


def test_multiple_subgroup_levels_compose_company_to_team(project: Path) -> None:
    gitlab = _GitLab(
        {
            "acme/apm-policy": _ACME,
            "acme/dept-a/apm-policy": _DEPT,
            "acme/dept-a/team-x/apm-policy": _TEAM,
        }
    )

    result = _discover(project, gitlab)

    assert _deny(result) == {"evil/pkg", "dept/pkg", "team/pkg"}
    assert result.chain_sources == [
        "gitlab.com/acme/apm-policy",
        "gitlab.com/acme/dept-a/apm-policy",
        "gitlab.com/acme/dept-a/team-x/apm-policy",
    ]


@pytest.mark.parametrize(
    "team",
    [
        "name: team\n",
        "name: team\nenforcement: off\ndependencies:\n  allow: [evil/pkg]\n",
    ],
    ids=["empty-child", "weaker-child"],
)
def test_empty_or_weaker_child_keeps_ancestor_restrictions(project: Path, team: str) -> None:
    gitlab = _GitLab({"acme/apm-policy": _ACME, "acme/dept-a/team-x/apm-policy": team})

    result = _discover(project, gitlab)

    assert result.policy is not None
    assert "evil/pkg" in _deny(result)
    assert result.policy.enforcement == "block"


def test_explicit_extends_to_discovered_ancestor_is_deduplicated(project: Path) -> None:
    """A child naming an already-discovered ancestor is not refetched or a cycle."""
    gitlab = _GitLab(
        {
            "acme/apm-policy": _ACME,
            "acme/dept-a/apm-policy": _policy("dept", deny="dept/pkg", extends="org"),
            "acme/dept-a/team-x/apm-policy": _policy(
                "team", deny="team/pkg", extends="acme/dept-a/apm-policy"
            ),
        }
    )

    result = _discover(project, gitlab)

    assert result.outcome == "found"
    assert _deny(result) == {"evil/pkg", "dept/pkg", "team/pkg"}
    assert sorted(gitlab.calls) == sorted(set(gitlab.calls))
    assert result.chain_sources == [
        "gitlab.com/acme/apm-policy",
        "gitlab.com/acme/dept-a/apm-policy",
        "gitlab.com/acme/dept-a/team-x/apm-policy",
    ]


def test_ancestor_extending_a_nearer_namespace_is_a_cycle(project: Path) -> None:
    gitlab = _GitLab(
        {
            "acme/apm-policy": _policy(
                "company", deny="evil/pkg", extends="acme/dept-a/apm-policy"
            ),
            "acme/dept-a/apm-policy": _DEPT,
        }
    )

    with pytest.raises(PolicyInheritanceError, match="Cycle detected"):
        _discover(project, gitlab)


def test_two_levels_extending_the_same_ancestor_compose_it_once(project: Path) -> None:
    """Team and department both ``extends: org``: no false cycle, no duplicate."""
    gitlab = _GitLab(
        {
            "acme/apm-policy": _ACME,
            "acme/dept-a/apm-policy": _policy("dept", deny="dept/pkg", extends="org"),
            "acme/dept-a/team-x/apm-policy": _policy("team", deny="team/pkg", extends="org"),
        }
    )

    result = _discover(project, gitlab)

    assert result.outcome == "found"
    assert _deny(result) == {"evil/pkg", "dept/pkg", "team/pkg"}
    assert gitlab.calls.count("acme/apm-policy") == 1
    assert result.chain_sources == [
        "gitlab.com/acme/apm-policy",
        "gitlab.com/acme/dept-a/apm-policy",
        "gitlab.com/acme/dept-a/team-x/apm-policy",
    ]


def test_explicit_cycle_through_discovered_ancestor_is_rejected(project: Path) -> None:
    gitlab = _GitLab(
        {
            "acme/dept-a/apm-policy": _policy(
                "dept", deny="dept/pkg", extends="acme/dept-a/team-x/apm-policy"
            ),
            "acme/dept-a/team-x/apm-policy": _policy(
                "team", deny="team/pkg", extends="acme/dept-a/apm-policy"
            ),
        }
    )

    with pytest.raises(PolicyInheritanceError, match="Cycle detected"):
        _discover(project, gitlab)


def test_five_policy_limit_counts_the_composed_chain(project: Path) -> None:
    levels = ["a", "a/b", "a/b/c", "a/b/c/d", "a/b/c/d/e", "a/b/c/d/e/f"]
    gitlab = _GitLab(
        {f"{level}/apm-policy": _policy(level, deny=f"x/{i}") for i, level in enumerate(levels)}
    )

    with pytest.raises(PolicyInheritanceError, match="exceeds maximum of 5"):
        _discover(project, gitlab, remote="https://gitlab.com/a/b/c/d/e/f/svc.git")


def test_ancestor_transport_failure_fails_closed_under_warn(project: Path) -> None:
    gitlab = _GitLab({"acme/apm-policy": 500, "acme/dept-a/team-x/apm-policy": _TEAM})

    result = _discover(project, gitlab)

    assert result.outcome == "incomplete_chain"
    assert result.policy is None
    assert "gitlab.com/acme/apm-policy" in result.error


def test_composed_chain_is_cached_and_served_without_probes(project: Path) -> None:
    projects = {"acme/apm-policy": _ACME, "acme/dept-a/team-x/apm-policy": _TEAM}
    _discover(project, _GitLab(projects))

    warm = _GitLab(projects)
    result = _discover(project, warm)

    assert warm.calls == []
    assert result.cached is True
    assert _deny(result) == {"evil/pkg", "team/pkg"}
    assert result.source == "org:gitlab.com/acme/dept-a/team-x/apm-policy"
    assert result.chain_sources[0] == "gitlab.com/acme/apm-policy"


def test_cache_only_serves_composed_chain_without_network(project: Path) -> None:
    projects = {"acme/apm-policy": _ACME, "acme/dept-a/team-x/apm-policy": _TEAM}
    _discover(project, _GitLab(projects))
    offline = _GitLab({})

    result = _discover(project, offline, cache_only=True)

    assert offline.calls == []
    assert _deny(result) == {"evil/pkg", "team/pkg"}


def test_cache_only_without_composed_cache_does_not_drop_layers(project: Path) -> None:
    """Per-namespace entries alone never stand in for the composed chain."""
    _discover(project, _GitLab({"acme/apm-policy": _ACME}), remote="https://gitlab.com/acme/x.git")
    offline = _GitLab({})

    result = _discover(project, offline, cache_only=True)

    assert offline.calls == []
    assert result.policy is None
    assert result.outcome == "absent"


def test_refresh_failure_serves_complete_stale_composition(project: Path) -> None:
    projects = {"acme/apm-policy": _ACME, "acme/dept-a/team-x/apm-policy": _TEAM}
    _discover(project, _GitLab(projects))
    later = time.time() + 2 * discovery.DEFAULT_CACHE_TTL

    with patch("apm_cli.policy.discovery.time.time", return_value=later):
        result = _discover(project, _GitLab({**projects, "acme/apm-policy": 503}))

    assert result.outcome == "cached_stale"
    assert _deny(result) == {"evil/pkg", "team/pkg"}
    assert result.fetch_error


def _pin(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_pin_verifies_nearest_policy_only(project: Path) -> None:
    gitlab = _GitLab({"acme/apm-policy": _ACME, "acme/dept-a/team-x/apm-policy": _TEAM})

    result = _discover(project, gitlab, expected_hash=_pin(_TEAM))

    assert result.outcome == "found"
    assert _deny(result) == {"evil/pkg", "team/pkg"}


def test_new_closer_policy_never_silently_repins(project: Path) -> None:
    projects = {"acme/apm-policy": _ACME, "acme/dept-a/apm-policy": _DEPT}
    pin = _pin(_DEPT)
    assert _discover(project, _GitLab(projects), expected_hash=pin).outcome == "found"

    closer = {**projects, "acme/dept-a/team-x/apm-policy": _TEAM}
    result = _discover(project, _GitLab(closer), expected_hash=pin, no_cache=True)

    assert result.outcome == "hash_mismatch"
    assert result.policy is None
