"""Wave 4: error UX surfacing for the copilot-app integrator.

Verifies that when ``copilot_app_db.deploy_workflow`` raises one of the
typed errors mid-install, the integrator:

1. Skips the failing prompt instead of crashing the run.
2. Surfaces an actionable diagnostic via the diagnostics collector,
   carrying the exception message so the user can act on it.
3. Continues with the next prompt (errors are per-prompt, not fatal).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.integration import copilot_app_db as db_mod
from apm_cli.integration.copilot_app_db import (
    CopilotAppDbLockedError,
    CopilotAppDbMissingError,
    CopilotAppDbSchemaError,
)
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS

SCHEDULED_PROMPT = """---
name: Daily Digest
schedule:
  interval: daily
  schedule_hour: 9
  mode: interactive
---
Summarise yesterday's commits.
"""

SCHEDULED_PROMPT_2 = """---
name: Hourly Heartbeat
schedule:
  interval: hourly
  mode: interactive
---
Hourly heartbeat body.
"""


class _CapturingDiagnostics:
    def __init__(self):
        self.warns: list[dict] = []

    def warn(self, **kwargs):
        self.warns.append(kwargs)


def _make_pkg(tmp_path: Path) -> SimpleNamespace:
    """Build a minimal package_info with two scheduled prompts."""
    pkg_dir = tmp_path / "pkg"
    prompts = pkg_dir / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "daily-digest.prompt.md").write_text(SCHEDULED_PROMPT)
    (prompts / "hourly-heartbeat.prompt.md").write_text(SCHEDULED_PROMPT_2)
    return SimpleNamespace(
        install_path=pkg_dir,
        package=SimpleNamespace(
            name="demo-pkg",
            source="github:acme-org/demo-pkg",
            author=None,
        ),
    )


@pytest.fixture
def copilot_app_target():
    profile = KNOWN_TARGETS.get("copilot-app")
    assert profile is not None, "copilot-app target must be registered"
    return profile


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    """Ensure ``resolve_copilot_app_db_path`` returns a valid path so
    the integrator proceeds past its defensive None-guard.  The DB file
    itself is irrelevant -- ``deploy_workflow`` will be monkeypatched."""
    db_file = tmp_path / "data.db"
    db_file.touch()
    monkeypatch.setenv("APM_COPILOT_APP_DB", str(db_file))
    return db_file


class TestDeployErrorSurfacing:
    @pytest.mark.parametrize(
        "exc_cls,exc_args,expected_substring",
        [
            (
                CopilotAppDbMissingError,
                ("~/.copilot/data.db not found",),
                "data.db not found",
            ),
            (
                CopilotAppDbSchemaError,
                ("user_version 99 is newer than tested 13",),
                "user_version 99",
            ),
            (
                CopilotAppDbLockedError,
                ("database is locked after 5s",),
                "database is locked",
            ),
        ],
    )
    def test_typed_errors_become_actionable_diagnostics(
        self,
        tmp_path,
        monkeypatch,
        fake_db,
        copilot_app_target,
        exc_cls,
        exc_args,
        expected_substring,
    ):
        """Each typed DB error surfaces as a per-prompt diagnostic warn
        carrying the original message; install does NOT raise."""
        pkg = _make_pkg(tmp_path)
        diags = _CapturingDiagnostics()

        def boom(*_args, **_kwargs):
            raise exc_cls(*exc_args)

        monkeypatch.setattr(db_mod, "deploy_workflow", boom)

        result = PromptIntegrator().integrate_prompts_for_target(
            copilot_app_target,
            pkg,
            project_root=tmp_path,
            diagnostics=diags,
        )

        # Both prompts failed -> both skipped, neither integrated.
        assert result.files_integrated == 0
        assert result.files_skipped == 2

        # Two diagnostics, one per prompt, each carrying the original
        # message so the user has something actionable.
        assert len(diags.warns) == 2
        for entry in diags.warns:
            assert entry["package"] == "demo-pkg"
            assert "Copilot App" in entry["message"]
            assert expected_substring in entry["message"]

    def test_partial_failure_does_not_block_subsequent_prompts(
        self,
        tmp_path,
        monkeypatch,
        fake_db,
        copilot_app_target,
    ):
        """First prompt fails with a locked DB; second prompt succeeds.
        Counters must reflect 1 integrated + 1 skipped."""
        pkg = _make_pkg(tmp_path)
        diags = _CapturingDiagnostics()
        call_count = {"n": 0}

        def flaky(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise CopilotAppDbLockedError("transient lock")

        monkeypatch.setattr(db_mod, "deploy_workflow", flaky)

        result = PromptIntegrator().integrate_prompts_for_target(
            copilot_app_target,
            pkg,
            project_root=tmp_path,
            diagnostics=diags,
        )

        assert result.files_integrated == 1
        assert result.files_skipped == 1
        assert len(diags.warns) == 1
        assert "transient lock" in diags.warns[0]["message"]

    def test_missing_db_resolver_returns_empty_no_exception(
        self,
        tmp_path,
        monkeypatch,
        copilot_app_target,
    ):
        """When the resolver returns None mid-run (e.g. DB deleted
        between gating and integration), the integrator is defensive:
        no crash, no diagnostics, empty result.  CLI gating in
        install/phases/targets.py is the surface that emits the
        actionable user-facing error."""
        from apm_cli.integration import prompt_integrator as pi_mod

        pkg = _make_pkg(tmp_path)
        diags = _CapturingDiagnostics()

        # Force the resolver path used INSIDE the integrator to None.
        # The function is imported locally inside
        # _integrate_prompts_for_copilot_app, so we patch on the db
        # module.
        monkeypatch.setattr(
            db_mod,
            "resolve_copilot_app_db_path",
            lambda: None,
        )

        # Sanity: ensure we are exercising the real integrator branch.
        assert hasattr(pi_mod.PromptIntegrator, "_integrate_prompts_for_copilot_app")

        result = PromptIntegrator().integrate_prompts_for_target(
            copilot_app_target,
            pkg,
            project_root=tmp_path,
            diagnostics=diags,
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 0
        assert diags.warns == []
