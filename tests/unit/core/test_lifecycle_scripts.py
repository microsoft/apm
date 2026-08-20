"""Unit tests for lifecycle script models, runner, and file-based discovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from apm_cli.core.lifecycle_scripts import (
    LIFECYCLE_EVENTS,
    SCRIPT_TYPES,
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
    ScriptEntry,
    _entries_from_lifecycle_map,
    build_runner_from_context,
    discover_scripts,
    parse_apm_yml_lifecycle,
    parse_project_script_file,
    parse_script_file,
)


def _write_yaml(path: Path, data: dict) -> Path:
    """Write *data* as YAML to *path* and return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
    return path


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
        assert payload["event"] == "post-install"
        assert payload["packages"] == [{"name": "org/repo", "reference": "v1"}]

    @patch("apm_cli.version.get_version", return_value="0.14.1")
    def test_create_factory_fills_version_and_timestamp(self, _mock_ver: MagicMock) -> None:
        event = LifecycleEvent.create(event="pre-install", packages=[PackageInfo(name="a/b")])
        assert event.event == "pre-install"
        assert event.cli_version == "0.14.1"
        assert event.timestamp


class TestScriptEntry:
    def test_effective_command_prefers_bash_on_unix(self) -> None:
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="./bash.sh",
            command="echo fallback",
        )
        with patch("platform.system", return_value="Linux"):
            assert script.effective_command == "./bash.sh"

    def test_effective_command_prefers_command_on_windows(self) -> None:
        script = ScriptEntry(
            script_type="command",
            event="post-install",
            bash="./bash.sh",
            command="powershell -File x.ps1",
        )
        with patch("platform.system", return_value="Windows"):
            assert script.effective_command == "powershell -File x.ps1"

    def test_effective_timeout_defaults(self) -> None:
        assert ScriptEntry(script_type="http", event="post-install").effective_timeout == 10
        assert ScriptEntry(script_type="command", event="post-install").effective_timeout == 30


class TestParseScriptFile:
    def test_parses_valid_json_file(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "scripts": {
                        "post-install": [
                            {"type": "command", "bash": "echo done"},
                            {"type": "http", "url": "https://x.com/script"},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        entries = parse_script_file(script_file)
        assert len(entries) == 2
        assert entries[0].bash == "echo done"
        assert entries[1].url == "https://x.com/script"

    def test_rejects_wrong_version(self, tmp_path: Path) -> None:
        script_file = tmp_path / "scripts.json"
        script_file.write_text(
            json.dumps({"version": 99, "scripts": {"post-install": []}}),
            encoding="utf-8",
        )
        assert parse_script_file(script_file) == []


class TestParseApmYmlLifecycle:
    def test_parses_lifecycle_block(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            {
                "name": "demo",
                "lifecycle": {
                    "post-install": [{"type": "command", "bash": "echo done"}],
                    "pre-uninstall": [{"type": "http", "url": "https://example.com/hook"}],
                },
            },
        )
        entries = parse_apm_yml_lifecycle(path, "project")
        assert len(entries) == 2
        assert entries[0].source == "project"
        assert entries[1].script_type == "http"

    def test_run_alias_populates_bash_and_command(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "run": "echo alias"}]}},
        )
        entry = parse_apm_yml_lifecycle(path, "project")[0]
        assert entry.bash == "echo alias"
        assert entry.command == "echo alias"

    def test_entries_from_lifecycle_map_needs_no_version(self, tmp_path: Path) -> None:
        entries = _entries_from_lifecycle_map(
            {"post-install": [{"type": "command", "bash": "echo ok"}]},
            tmp_path / "apm.yml",
            "project",
        )
        assert len(entries) == 1
        assert entries[0].event == "post-install"

    def test_parse_project_script_file_is_alias(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo ok"}]}},
        )
        assert parse_project_script_file(path) == parse_apm_yml_lifecycle(path, "project")


class TestDiscoverScripts:
    def test_discovers_from_project_file(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo project"}]}},
        )
        entries = discover_scripts(project_root=str(tmp_path))
        assert any(entry.bash == "echo project" for entry in entries)

    def test_discovers_from_user_apm_yml(self, tmp_path: Path) -> None:
        user_apm = _write_yaml(
            tmp_path / "user" / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo user"}]}},
        )
        with patch("apm_cli.core.lifecycle_scripts._get_user_apm_yml", return_value=user_apm):
            entries = discover_scripts()
        assert any(entry.bash == "echo user" for entry in entries)

    def test_additive_across_sources(self, tmp_path: Path) -> None:
        user_apm = _write_yaml(
            tmp_path / "user" / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo user"}]}},
        )
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        _write_yaml(
            project_dir / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo project"}]}},
        )
        with patch("apm_cli.core.lifecycle_scripts._get_user_apm_yml", return_value=user_apm):
            entries = discover_scripts(project_root=str(project_dir))
        assert len(entries) == 2


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

    def test_error_isolation_does_not_block_other_scripts(self) -> None:
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
        assert call_count == 2

    def test_scripts_for_event_filters(self) -> None:
        s1 = ScriptEntry(script_type="command", event="post-install", bash="echo a")
        s2 = ScriptEntry(script_type="http", event="pre-install", url="https://x.com")
        runner = LifecycleScriptRunner(scripts=[s1, s2])
        assert runner.scripts_for_event("post-install") == [s1]


class TestBuildRunnerFromContext:
    def test_apm_no_scripts_env_returns_empty_runner(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("APM_NO_SCRIPTS", "1")
        runner = build_runner_from_context(project_root=str(tmp_path))
        assert runner._scripts == []

    def test_deny_all_true_suppresses_all_scripts(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo hi"}]}},
        )
        mock_policy = MagicMock()
        mock_policy.executables.deny_all = True
        mock_fetch = MagicMock()
        mock_fetch.policy = mock_policy
        with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=mock_fetch):
            runner = build_runner_from_context(project_root=str(tmp_path))
        assert runner._scripts == []

    def test_deny_all_false_does_not_suppress_user_scripts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
        mock_policy = MagicMock()
        mock_policy.executables.deny_all = False
        mock_fetch = MagicMock()
        mock_fetch.policy = mock_policy
        user_apm = _write_yaml(
            tmp_path / "user" / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo user"}]}},
        )
        with (
            patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=mock_fetch),
            patch("apm_cli.core.lifecycle_scripts._get_user_apm_yml", return_value=user_apm),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))
        assert any(script.bash == "echo user" for script in runner._scripts)

    def test_untrusted_project_scripts_are_skipped(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo from-project"}]}},
        )
        with (
            patch("apm_cli.core.script_trust.is_fingerprint_trusted", return_value=False),
            patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))
        assert runner._scripts == []
        assert runner._skipped_project_scripts == 1

    def test_trusted_project_scripts_are_included(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo trusted"}]}},
        )
        with (
            patch("apm_cli.core.script_trust.is_fingerprint_trusted", return_value=True),
            patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))
        assert any(script.bash == "echo trusted" for script in runner._scripts)
        assert runner._skipped_project_scripts == 0

    def test_user_scripts_bypass_trust_gate(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
        user_apm = _write_yaml(
            tmp_path / "user" / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo user-notify"}]}},
        )
        with (
            patch("apm_cli.core.lifecycle_scripts._get_user_apm_yml", return_value=user_apm),
            patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None),
        ):
            runner = build_runner_from_context(project_root=str(tmp_path))
        assert any(script.bash == "echo user-notify" for script in runner._scripts)


class TestConstants:
    def test_lifecycle_events_tuple(self) -> None:
        assert set(LIFECYCLE_EVENTS) == {
            "pre-install",
            "post-install",
            "pre-update",
            "post-update",
            "pre-uninstall",
            "post-uninstall",
        }

    def test_script_types_tuple(self) -> None:
        assert set(SCRIPT_TYPES) == {"command", "http"}
