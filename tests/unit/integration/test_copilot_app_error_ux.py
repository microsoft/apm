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
interval: daily
schedule_hour: 9
mode: interactive
---
Summarise yesterday's commits.
"""

SCHEDULED_PROMPT_2 = """---
name: Hourly Heartbeat
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

        # Two per-prompt deploy diagnostics, each carrying the original
        # message so the user has something actionable. (An additional
        # "no git repo detected" warn fires once for the whole install
        # because tmp_path is not a git working tree -- filter it out.)
        deploy_warns = [w for w in diags.warns if expected_substring in w["message"]]
        assert len(deploy_warns) == 2
        for entry in deploy_warns:
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
        # One deploy warn for the transient lock; an additional "no git
        # repo detected" warn fires once because tmp_path is not a git
        # working tree -- filter it out.
        deploy_warns = [w for w in diags.warns if "transient lock" in w["message"]]
        assert len(deploy_warns) == 1
        assert "transient lock" in deploy_warns[0]["message"]

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


# ---------------------------------------------------------------------------
# Option B: dispatch-by-shape regression tests
# ---------------------------------------------------------------------------

PLAIN_PROMPT = """---
name: Plain Hello
description: A regular slash-command prompt with no execution metadata.
---
Say hello.
"""


class TestDispatchByShape:
    def test_plain_prompt_at_copilot_app_warns_hard(
        self,
        copilot_app_target,
        fake_db,
        tmp_path,
        monkeypatch,
    ):
        """A .prompt.md with NO workflow-shape keys, sent to --target
        copilot-app, must surface an actionable diagnostic explaining
        what to add or where to send it instead -- not silently skip."""
        pkg_dir = tmp_path / "pkg"
        prompts = pkg_dir / ".apm" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "plain.prompt.md").write_text(PLAIN_PROMPT)
        pkg = SimpleNamespace(
            install_path=pkg_dir,
            package=SimpleNamespace(
                name="demo-pkg",
                source="github:acme-org/demo-pkg",
                author=None,
            ),
        )
        diags = _CapturingDiagnostics()

        result = PromptIntegrator().integrate_prompts_for_target(
            copilot_app_target,
            pkg,
            project_root=tmp_path,
            diagnostics=diags,
        )

        assert result.files_integrated == 0
        assert result.files_skipped == 1
        assert len(diags.warns) == 1
        msg = diags.warns[0]["message"]
        # Diagnostic must name the shape requirement and the workaround.
        assert "no workflow frontmatter" in msg
        assert "interval" in msg
        assert "copilot-app" in msg

    def test_workflow_shape_skipped_by_slash_command_integrator(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The slash-command leak regression: a workflow-shape .prompt.md
        (interval/mode/etc) must NOT ship to .claude/commands/,
        .cursor/commands/, .copilot/prompts/, .gemini/commands/.  Without
        the shape-based skip in CommandIntegrator, a single source file
        used to deploy to 4 slash-command surfaces in addition to the
        Copilot App DB row."""
        from apm_cli.integration.command_integrator import CommandIntegrator
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = tmp_path / "pkg"
        prompts = pkg_dir / ".apm" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "scheduled.prompt.md").write_text(SCHEDULED_PROMPT)
        (prompts / "plain.prompt.md").write_text(PLAIN_PROMPT)
        pkg = SimpleNamespace(
            install_path=pkg_dir,
            package=SimpleNamespace(
                name="demo-pkg",
                source="github:acme-org/demo-pkg",
                author=None,
            ),
        )
        # Probe every file-based command target that should observe the
        # skip.  The integrator must deploy ONLY the plain prompt to
        # each; the workflow-shape one must be skipped.
        for target_name in ("claude", "cursor", "gemini"):
            profile = KNOWN_TARGETS.get(target_name)
            if profile is None or profile.primitives.get("prompts") is None:
                continue
            project_root = tmp_path / f"proj-{target_name}"
            project_root.mkdir()
            # Some targets are auto_create=False; create the dir so we
            # exercise the dispatch logic rather than the early-return.
            (project_root / profile.root_dir).mkdir(parents=True, exist_ok=True)
            result = CommandIntegrator().integrate_commands_for_target(
                profile,
                pkg,
                project_root=project_root,
                diagnostics=_CapturingDiagnostics(),
            )
            target_filenames = [p.name for p in result.target_paths]
            assert not any("scheduled" in n for n in target_filenames), (
                f"target {target_name!r} should NOT receive workflow-shape "
                f"prompt; got {target_filenames}"
            )

    def test_workflow_shape_skipped_by_copilot_prompt_integrator(
        self,
        tmp_path,
        monkeypatch,
    ):
        """The .github/prompts/ leak regression: a workflow-shape
        .prompt.md must NOT ship to .github/prompts/ when --target
        includes ``copilot`` (which routes through PromptIntegrator, not
        CommandIntegrator).  A user running
        ``--target copilot,copilot-app`` must see workflow metadata
        only in the App and plain prompts only in .github/prompts/.
        Without the shape-based skip in
        PromptIntegrator._integrate_prompts_for_copilot, scheduled
        prompts would leak into the slash-command surface and be
        invoked by IDE users who never opted into the workflow."""
        pkg_dir = tmp_path / "pkg"
        prompts = pkg_dir / ".apm" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "scheduled.prompt.md").write_text(SCHEDULED_PROMPT)
        (prompts / "plain.prompt.md").write_text(PLAIN_PROMPT)
        pkg = SimpleNamespace(
            install_path=pkg_dir,
            package=SimpleNamespace(
                name="demo-pkg",
                source="github:acme-org/demo-pkg",
                author=None,
            ),
        )
        copilot_profile = KNOWN_TARGETS.get("copilot")
        assert copilot_profile is not None
        project_root = tmp_path / "proj"
        project_root.mkdir()

        result = PromptIntegrator().integrate_prompts_for_target(
            copilot_profile,
            pkg,
            project_root=project_root,
            diagnostics=_CapturingDiagnostics(),
        )

        prompts_dir = project_root / ".github" / "prompts"
        written = [p.name for p in prompts_dir.rglob("*.prompt.md")] if prompts_dir.exists() else []
        assert not any("scheduled" in name for name in written), (
            f"workflow-shape prompt must NOT leak into .github/prompts/; got {written}"
        )
        # And the plain prompt SHOULD still deploy normally.
        assert any("plain" in name for name in written), (
            f"plain prompt should still deploy via --target copilot; got {written}"
        )
        # Sanity: the workflow-shape source was counted as skipped.
        assert result.files_skipped >= 1
