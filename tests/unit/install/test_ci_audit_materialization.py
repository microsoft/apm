"""``resolve_ci_audit_materialization`` decides where ``apm audit --ci`` reads packages.

Regression for #3781: a user workspace whose lockfile records no packages
(MCP/LSP-only, or only the workspace's own content) needs no
``$APM_HOME/apm_modules``, whether ``apm install -g`` or ``apm update -g``
wrote it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install import audit_replay
from apm_cli.install.audit_replay import resolve_ci_audit_materialization

_NO_PACKAGES = (
    "lockfile_version: '1'\napm_version: 0.36.0\ndependencies: []\nmcp_servers:\n- local-tool\n"
)
_ONE_PACKAGE = (
    "lockfile_version: '1'\n"
    "apm_version: 0.36.0\n"
    "dependencies:\n"
    "- repo_url: test/dep\n"
    "  host: github.com\n"
    "  resolved_ref: main\n"
    "  resolved_commit: 7835baab0000000000000000000000000000abcd\n"
    "  depth: 1\n"
    "  package_type: apm_package\n"
    "  deployed_files: []\n"
)
_SELF_CONTENT_ONLY = (
    "lockfile_version: '1'\n"
    "dependencies: []\n"
    "local_deployed_files:\n- .copilot/agents/kit-agent.agent.md\n"
)
_STAGED_BUNDLE_ONLY = (
    "lockfile_version: '1'\n"
    "dependencies: []\n"
    "local_staged_files:\n- apm_modules/my-bundle/.apm/instructions/a.instructions.md\n"
)


def _workspace(root: Path, lockfile: str) -> Path:
    root.mkdir(parents=True)
    (root / "apm.yml").write_text("name: ws\nversion: 1.0.0\n", encoding="utf-8")
    (root / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    return root


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "apm-home"
    monkeypatch.setenv("APM_HOME", str(home))
    return home


@pytest.fixture
def replay_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def _fake(project_root: Path, *, verbose: bool = False) -> str:
        calls.append(project_root)
        return "prepared"

    monkeypatch.setattr(audit_replay, "prepare_ci_audit_replay", _fake)
    return calls


@pytest.mark.parametrize(
    "lockfile",
    [_NO_PACKAGES, "lockfile_version: '1'\ndependencies: []\n", _SELF_CONTENT_ONLY],
    ids=["mcp-only", "empty", "self-content-only"],
)
def test_user_workspace_without_packages_needs_no_modules(
    user_home: Path, replay_calls: list[Path], lockfile: str
) -> None:
    root = _workspace(user_home, lockfile)

    result = resolve_ci_audit_materialization(root)

    assert (result.prepared_replay, result.error) == (None, None)
    assert replay_calls == []


@pytest.mark.parametrize(
    "lockfile", [_ONE_PACKAGE, _STAGED_BUNDLE_ONLY], ids=["package", "staging"]
)
def test_user_workspace_with_modules_content_reports_missing_materialization(
    user_home: Path, replay_calls: list[Path], lockfile: str
) -> None:
    root = _workspace(user_home, lockfile)

    result = resolve_ci_audit_materialization(root)

    assert result.prepared_replay is None
    assert result.error is not None
    assert "installed package materialization is missing at" in result.error
    assert str(root / "apm_modules") in result.error
    assert "apm install --global" in result.error
    assert replay_calls == []


def test_user_workspace_with_unreadable_lockfile_fails_closed(
    user_home: Path, replay_calls: list[Path]
) -> None:
    root = _workspace(user_home, "")

    result = resolve_ci_audit_materialization(root)

    assert result.error is not None and "materialization is missing" in result.error


@pytest.mark.parametrize("lockfile", [_NO_PACKAGES, _ONE_PACKAGE])
def test_live_modules_are_used_as_is(
    user_home: Path, tmp_path: Path, replay_calls: list[Path], lockfile: str
) -> None:
    for root in (_workspace(user_home, lockfile), _workspace(tmp_path / "project", lockfile)):
        (root / "apm_modules").mkdir()

        result = resolve_ci_audit_materialization(root)

        assert (result.prepared_replay, result.error) == (None, None)
    assert replay_calls == []


@pytest.mark.parametrize("lockfile", [_NO_PACKAGES, _ONE_PACKAGE])
def test_project_checkout_without_modules_keeps_scratch_replay(
    user_home: Path, tmp_path: Path, replay_calls: list[Path], lockfile: str
) -> None:
    root = _workspace(tmp_path / "project", lockfile)

    result = resolve_ci_audit_materialization(root)

    assert (result.prepared_replay, result.error) == ("prepared", None)
    assert replay_calls == [root]


def test_project_replay_failure_is_reported(
    user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path / "project", _ONE_PACKAGE)

    def _fail(project_root: Path, *, verbose: bool = False) -> None:
        raise audit_replay.CiAuditReplayError("cache unavailable")

    monkeypatch.setattr(audit_replay, "prepare_ci_audit_replay", _fail)

    result = resolve_ci_audit_materialization(root)

    assert (result.prepared_replay, result.error) == (None, "cache unavailable")


@pytest.mark.parametrize("missing", ["apm.yml", "apm.lock.yaml"])
def test_workspace_without_manifest_or_lockfile_is_left_to_baseline_checks(
    user_home: Path, replay_calls: list[Path], missing: str
) -> None:
    root = _workspace(user_home, _ONE_PACKAGE)
    (root / missing).unlink()

    result = resolve_ci_audit_materialization(root)

    assert (result.prepared_replay, result.error) == (None, None)
    assert replay_calls == []
