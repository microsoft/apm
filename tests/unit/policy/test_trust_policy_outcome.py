"""Executables-trust readers honour the policy discovery outcome (#4027)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, ExecutablesPolicy

_DISCOVER = "apm_cli.policy.discovery.discover_policy_with_chain"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "apm_cli.security.executables._user_config_file", lambda: tmp_path / "config.json"
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "apm.yml").write_text(
        "name: p\nversion: 1.0.0\nexecutables:\n  allow:\n    acme/hooky:\n      hooks: true\n",
        encoding="utf-8",
    )
    return root


def _block_on_fetch_failure(root: Path) -> None:
    manifest = root / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "policy:\n  fetch_failure_default: block\n",
        encoding="utf-8",
    )


def _discovery(monkeypatch: pytest.MonkeyPatch, result: PolicyFetchResult) -> list[dict]:
    calls: list[dict] = []

    def _discover(project_root, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(_DISCOVER, _discover)
    return calls


def _readers(root: Path):
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.install.exec_gate import ReplayExecAllowMap
    from apm_cli.install.local_bundle_handler import effective_bundle_allow_map
    from apm_cli.security.executables import resolve_project_exec_allow_map

    logger = CommandLogger("test")
    return {
        "project-map": lambda: resolve_project_exec_allow_map(
            root, SimpleNamespace(), user_scope=False, logger=logger
        ),
        "audit-replay": lambda: ReplayExecAllowMap(root).for_dependency(
            SimpleNamespace(exec_status="denied")
        ),
        "local-bundle": lambda: effective_bundle_allow_map(root, no_policy=False, logger=logger),
    }


_READERS = ("project-map", "audit-replay", "local-bundle")


@pytest.mark.parametrize("reader", _READERS)
@pytest.mark.parametrize("outcome", ["hash_mismatch", "incomplete_chain"])
def test_unverifiable_policy_fails_closed(
    project: Path, monkeypatch: pytest.MonkeyPatch, reader: str, outcome: str
) -> None:
    _discovery(monkeypatch, PolicyFetchResult(outcome=outcome, source="org:acme/.github"))
    with pytest.raises(PolicyViolationError):
        _readers(project)[reader]()


@pytest.mark.parametrize("reader", _READERS)
@pytest.mark.parametrize("outcome", ["cache_miss_fetch_fail", "absent"])
def test_unreachable_policy_follows_fetch_failure_default_block(
    project: Path, monkeypatch: pytest.MonkeyPatch, reader: str, outcome: str
) -> None:
    _block_on_fetch_failure(project)
    _discovery(monkeypatch, PolicyFetchResult(outcome=outcome, source="org:acme/.github"))
    with pytest.raises(PolicyViolationError, match="fetch_failure_default=block"):
        _readers(project)[reader]()


@pytest.mark.parametrize("reader", _READERS)
def test_unreachable_policy_under_warn_keeps_project_layers(
    project: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    _discovery(monkeypatch, PolicyFetchResult(outcome="cache_miss_fetch_fail"))
    allow = _readers(project)[reader]()
    assert allow is not None
    assert allow["acme/hooky"]["hooks"] is True


@pytest.mark.parametrize("reader", _READERS)
def test_found_policy_applies_the_org_layer(
    project: Path, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    policy = ApmPolicy(executables=ExecutablesPolicy(deny_all=True))
    _discovery(monkeypatch, PolicyFetchResult(outcome="found", policy=policy))
    allow = _readers(project)[reader]()
    assert allow is not None
    assert not allow.get("acme/hooky", {}).get("hooks")


def test_local_bundle_policy_is_not_cache_only(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _discovery(monkeypatch, PolicyFetchResult(outcome="absent"))
    _readers(project)["local-bundle"]()
    assert calls == [{}]


def test_warn_outcome_is_reported_through_a_plain_logger(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _discovery(monkeypatch, PolicyFetchResult(outcome="cache_miss_fetch_fail", fetch_error="x"))
    _readers(project)["project-map"]()
    out = capsys.readouterr()
    assert "policy" in (out.out + out.err).lower()
