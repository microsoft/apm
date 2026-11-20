"""Behavioral receipts for OpenAPM governance discovery requirements (req-pl-*).

Every test runs policy discovery against a real local git repository. The
network boundary (`_fetch_from_repo`, `_fetch_chain_parent`) is replaced by a
recorder, so the oracles are which sources discovery tries, in what order,
and what it returns.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apm_cli.policy import discovery
from apm_cli.policy._remote import _extract_org_host_port_from_git_remote
from apm_cli.policy.inheritance import PolicyInheritanceError
from apm_cli.policy.schema import ApmPolicy


def _git_project(root: Path, remotes: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(root), "remote", "add", name, url], check=True)
    return root


class _Fetches:
    """Records every policy source discovery tries and answers from a table."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, found: dict[str, str] | None = None):
        self.calls: list[str] = []
        self.found = found or {}
        monkeypatch.setattr(discovery, "_fetch_from_repo", self._repo)
        monkeypatch.setattr(discovery, "_fetch_chain_parent", self._parent)

    def _answer(self, ref: str) -> discovery.PolicyFetchResult:
        self.calls.append(ref)
        if ref in self.found:
            return discovery.PolicyFetchResult(
                policy=ApmPolicy(name=self.found[ref]), source=f"org:{ref}", outcome="found"
            )
        return discovery.PolicyFetchResult(source=f"org:{ref}", outcome="absent", not_found=True)

    def _repo(self, ref, *_args, **_kwargs):
        return self._answer(ref)

    def _parent(self, ref, **_kwargs):
        return self._answer(ref)


def _policy_file(root: Path, name: str, extends: str | None = None) -> Path:
    path = root / f"{name}.yml"
    body = f"name: {name}\nversion: 1.0.0\n"
    if extends:
        body += f"extends: {extends}\n"
    path.write_text(body, encoding="utf-8")
    return path


# --- req-pl-001: discovery order ---------------------------------------------


@pytest.mark.req("req-pl-001")
def test_explicit_policy_wins_and_skips_every_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _git_project(tmp_path / "app", {"origin": "https://github.com/acme/app.git"})
    fetches = _Fetches(monkeypatch, found={"acme/.github": "org-policy"})
    explicit = _policy_file(tmp_path, "explicit")

    result = discovery.discover_policy(project, policy_override=str(explicit), no_cache=True)

    assert result.policy is not None and result.policy.name == "explicit"
    assert fetches.calls == []


@pytest.mark.req("req-pl-001")
def test_providers_run_in_configured_order_and_first_policy_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _git_project(tmp_path / "app", {"origin": "https://github.com/acme/app.git"})
    fetches = _Fetches(monkeypatch, found={"acme/.github": "org-policy", "acme/.apm": "later"})

    result = discovery.discover_policy(project, no_cache=True)

    assert fetches.calls == ["acme/.github-private", "acme/.github"]
    assert result.policy is not None and result.policy.name == "org-policy"


@pytest.mark.req("req-pl-001")
def test_no_provider_yields_means_no_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _git_project(tmp_path / "app", {"origin": "https://github.com/acme/app.git"})
    fetches = _Fetches(monkeypatch)

    result = discovery.discover_policy(project, no_cache=True)

    assert fetches.calls == ["acme/.github-private", "acme/.github", "acme/.apm", "acme/_apm"]
    assert result.policy is None and result.outcome == "absent"


# --- req-pl-004: extends pinned to the leaf host -------------------------------


@pytest.mark.req("req-pl-004")
def test_cross_host_extends_is_rejected_before_any_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _git_project(tmp_path / "app", {"origin": "https://github.com/acme/app.git"})
    fetches = _Fetches(monkeypatch)
    leaf = _policy_file(tmp_path, "leaf", extends="evil.example.com/org/.github")

    with pytest.raises(PolicyInheritanceError, match="cross-host reference rejected"):
        discovery.discover_policy_with_chain(project, policy_override=str(leaf), no_cache=True)
    assert fetches.calls == []


@pytest.mark.req("req-pl-004")
@pytest.mark.parametrize(
    ("extends", "allowed"),
    [
        ("acme/.github", True),
        ("github.com/acme/.github", True),
        ("gitlab.com/acme/.github", False),
        ("https://evil.example.com/acme/.github", False),
    ],
)
def test_extends_host_must_match_the_leaf_host(extends: str, allowed: bool) -> None:
    if allowed:
        discovery._validate_extends_host("github.com", extends)
    else:
        with pytest.raises(PolicyInheritanceError, match="cross-host"):
            discovery._validate_extends_host("github.com", extends)


# --- req-pl-012: which git remote identifies the project ----------------------


def test_origin_is_preferred_and_no_remote_means_no_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing clauses of req-pl-012 (the id reports the xfails below)."""
    both = _git_project(
        tmp_path / "both",
        {
            "upstream": "https://github.com/other/app.git",
            "origin": "https://github.com/acme/app.git",
        },
    )
    assert _extract_org_host_port_from_git_remote(both) == ("acme", "github.com", None)
    fetches = _Fetches(monkeypatch)
    bare = _git_project(tmp_path / "bare", {})
    assert discovery.discover_policy(bare, no_cache=True).outcome == "no_git_remote"
    assert fetches.calls == []


@pytest.mark.req("req-pl-012")
@pytest.mark.xfail(strict=True, reason="#4279: only the origin remote is ever read")
def test_single_non_origin_remote_identifies_the_project(tmp_path: Path) -> None:
    project = _git_project(tmp_path / "app", {"upstream": "https://github.com/acme/app.git"})
    assert _extract_org_host_port_from_git_remote(project) == ("acme", "github.com", None)


@pytest.mark.req("req-pl-012")
@pytest.mark.xfail(strict=True, reason="#4279: only the origin remote is ever read")
def test_several_non_origin_remotes_fail_closed_naming_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _git_project(
        tmp_path / "app",
        {"fork": "https://github.com/me/app.git", "upstream": "https://github.com/acme/app.git"},
    )
    fetches = _Fetches(monkeypatch)
    result = discovery.discover_policy(project, no_cache=True)

    assert result.outcome not in ("absent", "no_git_remote", "found")
    assert "fork" in (result.error or "") and "upstream" in (result.error or "")
    assert fetches.calls == []
