"""Unit tests for lifecycle hook models, runner, and file-based discovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.core.lifecycle_hooks import (
    HOOK_TYPES,
    LIFECYCLE_EVENTS,
    HookEntry,
    LifecycleEvent,
    LifecycleHookRunner,
    PackageInfo,
    discover_hooks,
    parse_hook_file,
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


# -- HookEntry -------------------------------------------------------------


class TestHookEntry:
    def test_command_hook_effective_command_bash(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="./notify.sh")
        assert hook.effective_command == "./notify.sh"

    def test_command_hook_effective_command_fallback(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", command="echo done")
        assert hook.effective_command == "echo done"

    def test_command_hook_bash_takes_priority(self) -> None:
        hook = HookEntry(
            hook_type="command", event="post-install", bash="./bash.sh", command="echo fallback"
        )
        assert hook.effective_command == "./bash.sh"

    def test_http_hook_no_command(self) -> None:
        hook = HookEntry(hook_type="http", event="post-install", url="https://x.com")
        assert hook.effective_command is None

    def test_effective_timeout_http(self) -> None:
        hook = HookEntry(hook_type="http", event="post-install")
        assert hook.effective_timeout == 10

    def test_effective_timeout_command(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install")
        assert hook.effective_timeout == 30

    def test_effective_timeout_custom(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", timeout_sec=5)
        assert hook.effective_timeout == 5


# -- parse_hook_file -------------------------------------------------------


class TestParseHookFile:
    def test_parses_valid_file(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
                        "post-install": [
                            {"type": "command", "bash": "echo done"},
                            {"type": "http", "url": "https://x.com/hook"},
                        ],
                    },
                }
            )
        )
        hooks = parse_hook_file(hook_file)
        assert len(hooks) == 2
        assert hooks[0].hook_type == "command"
        assert hooks[0].bash == "echo done"
        assert hooks[1].hook_type == "http"
        assert hooks[1].url == "https://x.com/hook"

    def test_ignores_unknown_event(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(
            json.dumps(
                {"version": 1, "hooks": {"unknown-event": [{"type": "command", "bash": "x"}]}}
            )
        )
        assert parse_hook_file(hook_file) == []

    def test_ignores_unknown_type(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(
            json.dumps({"version": 1, "hooks": {"post-install": [{"type": "script", "path": "x"}]}})
        )
        assert parse_hook_file(hook_file) == []

    def test_rejects_wrong_version(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(
            json.dumps(
                {"version": 99, "hooks": {"post-install": [{"type": "command", "bash": "x"}]}}
            )
        )
        assert parse_hook_file(hook_file) == []

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text("not json")
        assert parse_hook_file(hook_file) == []

    def test_rejects_non_dict(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(json.dumps([1, 2, 3]))
        assert parse_hook_file(hook_file) == []

    def test_preserves_optional_fields(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
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
        hooks = parse_hook_file(hook_file)
        assert len(hooks) == 1
        assert hooks[0].cwd == "./scripts"
        assert hooks[0].env == {"FOO": "bar"}
        assert hooks[0].timeout_sec == 15

    def test_parses_http_headers(self, tmp_path: Path) -> None:
        hook_file = tmp_path / "hooks.json"
        hook_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
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
        hooks = parse_hook_file(hook_file)
        assert hooks[0].headers == {"Authorization": "Bearer $TOKEN"}


# -- discover_hooks ---------------------------------------------------------


class TestDiscoverHooks:
    def test_discovers_from_project_file(self, tmp_path: Path) -> None:
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        (apm_dir / "hooks.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {"post-install": [{"type": "command", "bash": "echo project"}]},
                }
            )
        )
        hooks = discover_hooks(project_root=str(tmp_path))
        assert len(hooks) >= 1
        assert any(h.bash == "echo project" for h in hooks)

    def test_discovers_from_user_dir(self, tmp_path: Path) -> None:
        user_hooks = tmp_path / "user_hooks"
        user_hooks.mkdir()
        (user_hooks / "global.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {"post-install": [{"type": "command", "bash": "echo user"}]},
                }
            )
        )
        with patch("apm_cli.core.lifecycle_hooks._get_user_hooks_dir", return_value=user_hooks):
            hooks = discover_hooks()
        assert any(h.bash == "echo user" for h in hooks)

    def test_additive_across_sources(self, tmp_path: Path) -> None:
        user_hooks = tmp_path / "user"
        user_hooks.mkdir()
        (user_hooks / "a.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {"post-install": [{"type": "command", "bash": "echo user"}]},
                }
            )
        )
        project_dir = tmp_path / "project"
        project_apm = project_dir / ".apm"
        project_apm.mkdir(parents=True)
        (project_apm / "hooks.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {"post-install": [{"type": "command", "bash": "echo project"}]},
                }
            )
        )
        with patch("apm_cli.core.lifecycle_hooks._get_user_hooks_dir", return_value=user_hooks):
            hooks = discover_hooks(project_root=str(project_dir))
        assert len(hooks) == 2

    def test_missing_project_file_returns_empty(self, tmp_path: Path) -> None:
        hooks = discover_hooks(project_root=str(tmp_path))
        # No policy or user hooks either in a clean tmp_path
        assert hooks == []

    def test_no_dirs_returns_empty(self) -> None:
        with (
            patch(
                "apm_cli.core.lifecycle_hooks._get_policy_hooks_dir",
                return_value=Path("/nonexistent"),
            ),
            patch(
                "apm_cli.core.lifecycle_hooks._get_user_hooks_dir",
                return_value=Path("/nonexistent2"),
            ),
        ):
            hooks = discover_hooks(project_root="/nonexistent3")
        assert hooks == []


# -- LifecycleHookRunner ---------------------------------------------------


class TestLifecycleHookRunner:
    def _make_event(self, event_name: str = "post-install") -> LifecycleEvent:
        return LifecycleEvent(
            event=event_name,
            packages=[PackageInfo(name="org/repo")],
            scope="project",
            timestamp="2026-01-01T00:00:00Z",
            cli_version="0.0.0",
            working_directory="/tmp/test",
        )

    def test_fire_calls_matching_hooks(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="echo hi")
        runner = LifecycleHookRunner(hooks=[hook])
        with patch("apm_cli.core.hook_executors.execute_hook") as mock_exec:
            runner.fire("post-install", self._make_event())
            mock_exec.assert_called_once()

    def test_fire_skips_non_matching_events(self) -> None:
        hook = HookEntry(hook_type="command", event="pre-install", bash="echo")
        runner = LifecycleHookRunner(hooks=[hook])
        with patch("apm_cli.core.hook_executors.execute_hook") as mock_exec:
            runner.fire("post-install", self._make_event())
            mock_exec.assert_not_called()

    def test_error_isolation_one_failing_hook_does_not_block_others(self) -> None:
        hook1 = HookEntry(hook_type="command", event="post-install", bash="fail")
        hook2 = HookEntry(hook_type="command", event="post-install", bash="ok")
        runner = LifecycleHookRunner(hooks=[hook1, hook2])
        call_count = 0

        def _side_effect(hook, event, **kw):
            nonlocal call_count
            call_count += 1
            if hook.bash == "fail":
                raise RuntimeError("boom")

        with patch("apm_cli.core.hook_executors.execute_hook", side_effect=_side_effect):
            runner.fire("post-install", self._make_event())
        assert call_count == 2  # both hooks were attempted

    def test_fire_with_no_hooks_is_noop(self) -> None:
        runner = LifecycleHookRunner(hooks=[])
        # Should not raise.
        runner.fire("post-install", self._make_event())

    def test_verbose_logs_on_failure(self) -> None:
        hook = HookEntry(hook_type="command", event="post-install", bash="bad")
        logger = MagicMock()
        runner = LifecycleHookRunner(hooks=[hook], logger=logger, verbose=True)
        with patch("apm_cli.core.hook_executors.execute_hook", side_effect=RuntimeError("boom")):
            runner.fire("post-install", self._make_event())
        logger.verbose_detail.assert_called_once()


# -- Constants --------------------------------------------------------------


class TestConstants:
    def test_lifecycle_events_tuple(self) -> None:
        assert "pre-install" in LIFECYCLE_EVENTS
        assert "post-install" in LIFECYCLE_EVENTS
        assert "pre-update" in LIFECYCLE_EVENTS
        assert "post-update" in LIFECYCLE_EVENTS
        assert "pre-uninstall" in LIFECYCLE_EVENTS
        assert "post-uninstall" in LIFECYCLE_EVENTS

    def test_hook_types_tuple(self) -> None:
        assert set(HOOK_TYPES) == {"command", "http"}


class TestHooksForEvent:
    def test_returns_matching_hooks(self) -> None:
        h1 = HookEntry(hook_type="command", event="post-install", bash="echo a")
        h2 = HookEntry(hook_type="command", event="pre-install", bash="echo b")
        h3 = HookEntry(hook_type="http", event="post-install", url="https://x.com")
        runner = LifecycleHookRunner(hooks=[h1, h2, h3])
        result = runner.hooks_for_event("post-install")
        assert result == [h1, h3]

    def test_returns_empty_for_unknown_event(self) -> None:
        h = HookEntry(hook_type="command", event="post-install", bash="echo")
        runner = LifecycleHookRunner(hooks=[h])
        assert runner.hooks_for_event("pre-uninstall") == []
