"""Unit tests for lifecycle script models, runner, and file-based discovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.core.lifecycle_scripts import (
    LIFECYCLE_EVENTS,
    SCRIPT_TYPES,
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
    ScriptEntry,
    build_runner_from_context,
    discover_scripts,
    parse_script_file,
)

# -- PackageInfo -----------------------------------------------------------


class TestPackageInfo:
    def test_defaults(self) -> None:
        pkg = PackageInfo(name="org/repo")
        assert pkg.name == "org/repo"
        assert pkg.reference is None

    def test_with_reference(self) -> None:
        pkg = PackageInfo(name="org/repo", reference="v1.0.0")
        assert pkg.reference == "v1.0.0"


# -- LifecycleEvent --------------------------------------------------------


class TestLifecycleEvent:
    def test_to_json_round_trips(self) -> None:
        event = LifecycleEvent(
            event="post-install",
            packages=[PackageInfo(name="org/repo", reference="v1")],
            scope="project",
            timestamp="2026-01-01T00:00:00Z",
            cli_version="0.14.0",
            working_directory="/tmp/project",
        )
        payload = json.loads(event.to_json())
        assert payload["schema_version"] == 1
        assert payload["event"] == "post-install"
        assert payload["packages"] == [{"name": "org/repo", "reference": "v1"}]
        assert payload["scope"] == "project"
        assert payload["working_directory"] == "/tmp/project"

    @patch("apm_cli.version.get_version", return_value="0.14.1")
    def test_create_factory_fills_version_and_timestamp(self, _mock_ver: MagicMock) -> None:
        event = LifecycleEvent.create(
            event="pre-install",
            packages=[PackageInfo(name="a/b")],
            scope="user",
        )
        assert event.event == "pre-install"
        assert event.cli_version == "0.14.1"
        assert event.timestamp  # non-empty ISO string
        assert event.scope == "user"

    @patch("apm_cli.version.get_version", return_value="0.0.0")
    def test_create_defaults_to_project_scope(self, _m: MagicMock) -> None:
        event = LifecycleEvent.create(event="post-install")
        assert event.scope == "project"
        assert event.packages == []

    @patch("apm_cli.version.get_version", return_value="0.0.0")
    def test_create_with_working_directory(self, _m: MagicMock) -> None:
        event = LifecycleEvent.create(event="post-install", working_directory="/my/project")
        assert event.working_directory == "/my/project"


# -- ScriptEntry -----------------------------------------------------------


class TestScriptEntry:
    def test_command_script_effective_command_bash(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="./notify.sh")
        assert script.effective_command == "./notify.sh"

    def test_command_script_effective_command_fallback(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", command="echo done")
        assert script.effective_command == "echo done"

    def test_command_script_bash_takes_priority(self) -> None:
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="./bash.sh",
            command="echo fallback",
        )
        assert script.effective_command == "./bash.sh"

    def test_http_script_no_command(self) -> None:
        script = ScriptEntry(script_type="http", event="post-install", url="https://x.com")
        assert script.effective_command is None

    def test_effective_timeout_http(self) -> None:
        script = ScriptEntry(script_type="http", event="post-install")
        assert script.effective_timeout == 10

    def test_effective_timeout_command(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install")
        assert script.effective_timeout == 30

    def test_effective_timeout_custom(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", timeout_sec=5)
        assert script.effective_timeout == 5


# -- parse_script_file -----------------------------------------------------


class TestParseScriptFile:
    def test_parses_valid_file(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {
                        "post-install": [
                            {"type": "command", "bash": "echo done"},
                            {"type": "http", "url": "https://x.com/script"},
                        ],
                    },
                }
            )
        )
        entries = parse_script_file(script_file)
        assert len(entries) == 2
        assert entries[0].script_type == "command"
        assert entries[0].bash == "echo done"
        assert entries[1].script_type == "http"
        assert entries[1].url == "https://x.com/script"

    def test_ignores_unknown_event(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"unknown-event": [{"type": "command", "bash": "x"}]},
                }
            )
        )
        assert parse_script_file(script_file) == []

    def test_ignores_unknown_type(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "shell", "path": "x"}]},
                }
            )
        )
        assert parse_script_file(script_file) == []

    def test_rejects_wrong_version(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 99,
                    "scripts": {"post-install": [{"type": "command", "bash": "x"}]},
                }
            )
        )
        assert parse_script_file(script_file) == []

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text("not json")
        assert parse_script_file(script_file) == []

    def test_rejects_non_dict(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(json.dumps([1, 2, 3]))
        assert parse_script_file(script_file) == []

    def test_preserves_optional_fields(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {
                        "post-install": [
                            {
                                "type": "command",
                                "bash": "echo",
                                "cwd": "./scripts",
                                "env": {"FOO": "bar"},
                                "timeoutSec": 15,
                            }
                        ],
                    },
                }
            )
        )
        entries = parse_script_file(script_file)
        assert len(entries) == 1
        assert entries[0].cwd == "./scripts"
        assert entries[0].env == {"FOO": "bar"}
        assert entries[0].timeout_sec == 15

    def test_parses_http_headers(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {
                        "post-install": [
                            {
                                "type": "http",
                                "url": "https://example.com",
                                "headers": {"Authorization": "Bearer $TOKEN"},
                            }
                        ],
                    },
                }
            )
        )
        entries = parse_script_file(script_file)
        assert entries[0].headers == {"Authorization": "Bearer $TOKEN"}


# -- discover_scripts ------------------------------------------------------


class TestDiscoverScripts:
    def test_discovers_from_project_file(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        (apm_dir / "scripts.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo project"}]},
                }
            )
        )
        entries = discover_scripts(project_root=str(tmp_path))
        assert len(entries) >= 1
        assert any(e.bash == "echo project" for e in entries)

    def test_discovers_from_user_dir(self, tmp_path: Path) -> None:
        user_scripts = tmp_path / "user_scripts"
        user_scripts.mkdir()
        (user_scripts / "global.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo user"}]},
                }
            )
        )
        with patch(
            "apm_cli.core.lifecycle_scripts._get_user_scripts_dir", return_value=user_scripts
        ):
            entries = discover_scripts()
        assert any(e.bash == "echo user" for e in entries)

    def test_additive_across_sources(self, tmp_path: Path) -> None:
        user_scripts = tmp_path / "user"
        user_scripts.mkdir()
        (user_scripts / "a.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo user"}]},
                }
            )
        )
        project_dir = tmp_path / "project"
        project_apm = project_dir / ".apm"
        project_apm.mkdir(parents=True)
        (project_apm / "scripts.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo project"}]},
                }
            )
        )
        with patch(
            "apm_cli.core.lifecycle_scripts._get_user_scripts_dir", return_value=user_scripts
        ):
            entries = discover_scripts(project_root=str(project_dir))
        assert len(entries) == 2

    def test_missing_project_file_returns_empty(self, tmp_path: Path) -> None:
        entries = discover_scripts(project_root=str(tmp_path))
        assert entries == []

    def test_no_dirs_returns_empty(self) -> None:
        with (
            patch(
                "apm_cli.core.lifecycle_scripts._get_policy_scripts_dir",
                return_value=Path("/nonexistent"),
            ),
            patch(
                "apm_cli.core.lifecycle_scripts._get_user_scripts_dir",
                return_value=Path("/nonexistent2"),
            ),
        ):
            entries = discover_scripts(project_root="/nonexistent3")
        assert entries == []


# -- LifecycleScriptRunner -------------------------------------------------


class TestLifecycleScriptRunner:
    def _make_event(self, event_name: str = "post-install") -> LifecycleEvent:
        return LifecycleEvent(
            event=event_name,
            packages=[PackageInfo(name="org/repo")],
            scope="project",
            timestamp="2026-01-01T00:00:00Z",
            cli_version="0.0.0",
            working_directory="/tmp/test",
        )

    def test_fire_calls_matching_scripts(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="echo hi")
        runner = LifecycleScriptRunner(scripts=[script])
        with patch("apm_cli.core.script_executors.execute_script") as mock_exec:
            runner.fire("post-install", self._make_event())
            mock_exec.assert_called_once()

    def test_fire_skips_non_matching_events(self) -> None:
        script = ScriptEntry(script_type="command", event="pre-install", bash="echo")
        runner = LifecycleScriptRunner(scripts=[script])
        with patch("apm_cli.core.script_executors.execute_script") as mock_exec:
            runner.fire("post-install", self._make_event())
            mock_exec.assert_not_called()

    def test_error_isolation_one_failing_script_does_not_block_others(self) -> None:
        script1 = ScriptEntry(script_type="command", event="post-install", bash="fail")
        script2 = ScriptEntry(script_type="command", event="post-install", bash="ok")
        runner = LifecycleScriptRunner(scripts=[script1, script2])
        call_count = 0

        def _side_effect(script, event, **kw):
            nonlocal call_count
            call_count += 1
            if script.bash == "fail":
                raise RuntimeError("boom")

        with patch("apm_cli.core.script_executors.execute_script", side_effect=_side_effect):
            runner.fire("post-install", self._make_event())
        assert call_count == 2  # both scripts were attempted

    def test_fire_with_no_scripts_is_noop(self) -> None:
        runner = LifecycleScriptRunner(scripts=[])
        runner.fire("post-install", self._make_event())

    def test_verbose_logs_on_failure(self) -> None:
        script = ScriptEntry(script_type="command", event="post-install", bash="bad")
        logger = MagicMock()
        runner = LifecycleScriptRunner(scripts=[script], logger=logger, verbose=True)
        with patch(
            "apm_cli.core.script_executors.execute_script", side_effect=RuntimeError("boom")
        ):
            runner.fire("post-install", self._make_event())
        logger.verbose_detail.assert_called_once()


# -- Constants -------------------------------------------------------------


class TestConstants:
    def test_lifecycle_events_tuple(self) -> None:
        assert "pre-install" in LIFECYCLE_EVENTS
        assert "post-install" in LIFECYCLE_EVENTS
        assert "pre-update" in LIFECYCLE_EVENTS
        assert "post-update" in LIFECYCLE_EVENTS
        assert "pre-uninstall" in LIFECYCLE_EVENTS
        assert "post-uninstall" in LIFECYCLE_EVENTS

    def test_script_types_tuple(self) -> None:
        assert set(SCRIPT_TYPES) == {"command", "http"}


class TestScriptsForEvent:
    def test_returns_matching_scripts(self) -> None:
        s1 = ScriptEntry(script_type="command", event="post-install", bash="echo a")
        s2 = ScriptEntry(script_type="command", event="pre-install", bash="echo b")
        s3 = ScriptEntry(script_type="http", event="post-install", url="https://x.com")
        runner = LifecycleScriptRunner(scripts=[s1, s2, s3])
        result = runner.scripts_for_event("post-install")
        assert result == [s1, s3]

    def test_returns_empty_for_unknown_event(self) -> None:
        s = ScriptEntry(script_type="command", event="post-install", bash="echo")
        runner = LifecycleScriptRunner(scripts=[s])
        assert runner.scripts_for_event("pre-uninstall") == []


# -- build_runner_from_context + org deny_all governance -------------------


class TestBuildRunnerFromContext:
    def test_apm_no_scripts_env_returns_empty_runner(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("APM_NO_SCRIPTS", "1")
        runner = build_runner_from_context(project_root=str(tmp_path))
        assert runner._scripts == []

    def test_deny_all_true_suppresses_all_scripts(self, tmp_path: Path, monkeypatch) -> None:
        """org executables.deny_all=True -> zero scripts fired regardless of source."""
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
        # Put a project script file in place (would otherwise be loaded)
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        script_file = apm_dir / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo hi"}]},
                }
            )
        )

        mock_policy = MagicMock()
        mock_policy.executables.deny_all = True
        mock_fetch = MagicMock()
        mock_fetch.policy = mock_policy

        with patch(
            "apm_cli.policy.discovery.discover_policy_with_chain",
            return_value=mock_fetch,
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))

        assert runner._scripts == []

    def test_deny_all_false_does_not_suppress_user_scripts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """org executables.deny_all=False -> scripts from non-project sources run."""
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        mock_policy = MagicMock()
        mock_policy.executables.deny_all = False
        mock_fetch = MagicMock()
        mock_fetch.policy = mock_policy

        user_scripts_dir = tmp_path / "user_scripts"
        user_scripts_dir.mkdir()
        (user_scripts_dir / "global.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo user"}]},
                }
            )
        )

        with (
            patch(
                "apm_cli.policy.discovery.discover_policy_with_chain",
                return_value=mock_fetch,
            ),
            patch(
                "apm_cli.core.lifecycle_scripts._get_user_scripts_dir",
                return_value=user_scripts_dir,
            ),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))

        assert any(s.bash == "echo user" for s in runner._scripts)

    def test_policy_discovery_error_does_not_raise(self, tmp_path: Path, monkeypatch) -> None:
        """Any exception from policy discovery is silently ignored."""
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        with patch(
            "apm_cli.policy.discovery.discover_policy_with_chain",
            side_effect=RuntimeError("network error"),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))

        # Should not have raised; runner is returned normally
        assert runner is not None

    def test_untrusted_project_scripts_are_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """Project scripts without trust record are excluded from the runner."""
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        script_file = apm_dir / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo from-project"}]},
                }
            )
        )

        # is_project_scripts_trusted returns False (no trust record)
        with (
            patch(
                "apm_cli.core.script_trust.is_project_scripts_trusted",
                return_value=False,
            ),
            patch(
                "apm_cli.policy.discovery.discover_policy_with_chain",
                return_value=None,
            ),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))

        assert runner._scripts == []
        assert runner._skipped_project_scripts == 1

    def test_trusted_project_scripts_are_included(self, tmp_path: Path, monkeypatch) -> None:
        """Project scripts with a valid trust record are included in the runner."""
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        script_file = apm_dir / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo trusted"}]},
                }
            )
        )

        with (
            patch(
                "apm_cli.core.script_trust.is_project_scripts_trusted",
                return_value=True,
            ),
            patch(
                "apm_cli.policy.discovery.discover_policy_with_chain",
                return_value=None,
            ),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))

        assert any(s.bash == "echo trusted" for s in runner._scripts)
        assert runner._skipped_project_scripts == 0

    def test_user_scripts_bypass_trust_gate(self, tmp_path: Path, monkeypatch) -> None:
        """User-source scripts are never subject to the project trust gate."""
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        user_scripts_dir = tmp_path / "user_scripts"
        user_scripts_dir.mkdir()
        (user_scripts_dir / "notify.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {"post-install": [{"type": "command", "bash": "echo user-notify"}]},
                }
            )
        )

        with (
            patch(
                "apm_cli.core.lifecycle_scripts._get_user_scripts_dir",
                return_value=user_scripts_dir,
            ),
            patch(
                "apm_cli.policy.discovery.discover_policy_with_chain",
                return_value=None,
            ),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))

        assert any(s.bash == "echo user-notify" for s in runner._scripts)
