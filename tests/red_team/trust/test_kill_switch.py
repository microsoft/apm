"""Vector 7 -- kill-switch interplay + the documented --execute bypass.

Covers:
  * APM_NO_SCRIPTS with non-empty values ("1", "0", "false") -- all disable
    (safe direction), documenting the "0"/"false" footgun.
  * org executables.deny_all suppresses even TRUSTED project scripts.
  * `apm lifecycle test --execute` fires through the same gate (#3983).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleScriptRunner,
    build_runner_from_context,
)
from apm_cli.core.script_trust import trust_project_scripts

PROJECT_YML = "lifecycle:\n  post-install:\n    - type: command\n      bash: echo hi\n"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(PROJECT_YML, encoding="utf-8")
    return project


@pytest.mark.usefixtures("hermetic_sources")
@pytest.mark.parametrize("value", ["1", "0", "false", "no", "  "])
def test_apm_no_scripts_any_nonempty_disables(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Any non-empty APM_NO_SCRIPTS disables scripts -- including "0"/"false".

    Safe direction (fails closed) but a footgun: "0" does NOT re-enable.
    """
    project = _project(tmp_path)
    trust_project_scripts(project / "apm.yml")
    monkeypatch.setenv("APM_NO_SCRIPTS", value)

    runner = build_runner_from_context(project_root=str(project))
    assert runner.scripts_for_event("post-install") == []


@pytest.mark.usefixtures("hermetic_sources")
def test_empty_apm_no_scripts_does_not_disable(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty-string APM_NO_SCRIPTS is falsy and does NOT disable scripts."""
    project = _project(tmp_path)
    trust_project_scripts(project / "apm.yml")
    monkeypatch.setenv("APM_NO_SCRIPTS", "")

    runner = build_runner_from_context(project_root=str(project))
    assert runner.scripts_for_event("post-install"), "empty value should be a no-op"


def test_org_deny_all_suppresses_even_trusted(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """org executables.deny_all is a one-way ceiling over trusted project scripts."""
    project = _project(tmp_path)
    trust_project_scripts(project / "apm.yml")

    empty_policy = tmp_path / "no_policy_dir"
    monkeypatch.setattr(
        "apm_cli.core.lifecycle_scripts._get_policy_scripts_dir", lambda: empty_policy
    )
    deny_result = SimpleNamespace(
        policy=SimpleNamespace(executables=SimpleNamespace(deny_all=True))
    )
    monkeypatch.setattr(
        "apm_cli.policy.discovery.discover_policy_with_chain",
        lambda *_a, **_k: deny_result,
    )

    runner = build_runner_from_context(project_root=str(project))
    assert runner.scripts_for_event("post-install") == [], (
        "deny_all must suppress even a trusted project script"
    )


def test_org_deny_all_reaches_runner_through_real_chain_discovery(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deny_all holds through the real discover_policy_with_chain with a str root.

    Only the leaf fetch is stubbed. The chain entry point, which joins
    ``apm.yml`` onto the root, runs for real: a str root used to raise a
    TypeError there that the runner swallowed, so deny_all never applied.
    """
    project = _project(tmp_path)
    trust_project_scripts(project / "apm.yml")
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.setattr(
        "apm_cli.core.lifecycle_scripts._get_policy_scripts_dir",
        lambda: tmp_path / "no_policy_dir",
    )
    leaf = SimpleNamespace(
        policy=SimpleNamespace(extends=None, executables=SimpleNamespace(deny_all=True)),
        cached=False,
    )
    roots: list[object] = []

    def _leaf(root, **_kwargs):
        roots.append(root)
        return leaf

    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy", _leaf)

    runner = build_runner_from_context(project_root=str(project))
    assert runner.scripts_for_event("post-install") == [], (
        "deny_all must suppress a trusted project script through real chain discovery"
    )
    assert roots == [project]


@pytest.mark.usefixtures("hermetic_sources")
def test_lifecycle_test_execute_goes_through_the_gate(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apm lifecycle test --execute` fires through build_runner_from_context,
    so APM_NO_SCRIPTS suppresses it like every other lifecycle command."""
    from click.testing import CliRunner

    from apm_cli.commands.lifecycle import lifecycle_test

    project = _project(tmp_path)
    trust_project_scripts(project / "apm.yml")
    monkeypatch.setenv("APM_NO_SCRIPTS", "1")
    monkeypatch.chdir(project)
    fired: list[str] = []
    monkeypatch.setattr(
        LifecycleScriptRunner, "fire", lambda self, *a, **k: fired.append("x") or []
    )

    result = CliRunner().invoke(lifecycle_test, ["post-install", "--execute"])

    assert result.exit_code == 0, result.output
    assert fired == [], "APM_NO_SCRIPTS must stop lifecycle test --execute"
