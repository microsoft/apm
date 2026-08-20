"""Vector 7 -- kill-switch interplay + the documented --execute bypass.

Covers:
  * APM_NO_SCRIPTS with non-empty values ("1", "0", "false") -- all disable
    (safe direction), documenting the "0"/"false" footgun.
  * org executables.deny_all suppresses even TRUSTED project scripts.
  * `apm lifecycle test --execute` intentionally bypasses the gate (the
    user typed --execute in their own repo) -- asserted as intended design,
    is_genuine_break=false.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleScriptRunner,
    build_runner_from_context,
    discover_scripts,
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


@pytest.mark.usefixtures("hermetic_sources")
def test_lifecycle_test_execute_bypasses_gate_by_design(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apm lifecycle test --execute` builds the runner directly from
    discover_scripts, deliberately bypassing both the trust gate and
    APM_NO_SCRIPTS.  This is the documented design (explicit user intent in
    their own repo), so we assert the bypass holds.  is_genuine_break=false.
    """
    project = _project(tmp_path)
    # No trust recorded; kill-switch set -- both would block build_runner_from_context.
    monkeypatch.setenv("APM_NO_SCRIPTS", "1")

    # Mirror the exact two lines apm lifecycle test --execute runs.
    all_scripts = discover_scripts(project_root=str(project))
    runner = LifecycleScriptRunner(scripts=all_scripts, project_root=str(project))

    matching = runner.scripts_for_event("post-install")
    assert matching, "lifecycle test --execute must see untrusted project scripts"
    assert any(s.source == "project" for s in matching)
