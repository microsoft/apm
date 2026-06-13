"""Unit tests for lifecycle hook models, runner, and discovery."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from apm_cli.core.lifecycle_hooks import (
    HOOK_TYPES,
    LIFECYCLE_EVENTS,
    HookDefinition,
    LifecycleEvent,
    LifecycleHookRunner,
    PackageInfo,
    collect_hooks,
    parse_hooks_from_config,
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
        )
        payload = json.loads(event.to_json())
        assert payload["schema_version"] == 1
        assert payload["event"] == "post-install"
        assert payload["packages"] == [{"name": "org/repo", "reference": "v1"}]
        assert payload["scope"] == "project"

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


# -- HookDefinition --------------------------------------------------------


class TestHookDefinition:
    def test_identity_key_command(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="echo hi")
        assert hook.identity_key == ("post-install", "command", "echo hi")

    def test_identity_key_webhook(self) -> None:
        hook = HookDefinition(
            hook_type="webhook", event="post-install", url="https://example.com/hook"
        )
        assert hook.identity_key == ("post-install", "webhook", "https://example.com/hook")

    def test_identity_key_script(self) -> None:
        hook = HookDefinition(hook_type="script", event="pre-install", path=".apm/hooks/pre.sh")
        assert hook.identity_key == ("pre-install", "script", ".apm/hooks/pre.sh")


# -- parse_hooks_from_config -----------------------------------------------


class TestParseHooksFromConfig:
    def test_parses_valid_config(self) -> None:
        raw = {
            "post-install": [
                {"type": "command", "run": "echo done"},
                {"type": "webhook", "url": "https://x.com", "token_env": "MY_TOK"},
            ],
        }
        hooks = parse_hooks_from_config(raw, source="project")
        assert len(hooks) == 2
        assert hooks[0].hook_type == "command"
        assert hooks[0].run == "echo done"
        assert hooks[0].source == "project"
        assert hooks[1].hook_type == "webhook"
        assert hooks[1].token_env == "MY_TOK"

    def test_ignores_unknown_event(self) -> None:
        raw = {"unknown-event": [{"type": "command", "run": "echo"}]}
        assert parse_hooks_from_config(raw) == []

    def test_ignores_unknown_type(self) -> None:
        raw = {"post-install": [{"type": "unknown_action", "run": "echo"}]}
        assert parse_hooks_from_config(raw) == []

    def test_ignores_non_dict_entries(self) -> None:
        raw = {"post-install": ["not-a-dict", 42]}
        assert parse_hooks_from_config(raw) == []

    def test_ignores_non_list_event_value(self) -> None:
        raw = {"post-install": "not-a-list"}
        assert parse_hooks_from_config(raw) == []

    def test_returns_empty_for_non_dict_input(self) -> None:
        assert parse_hooks_from_config("garbage") == []  # type: ignore[arg-type]

    def test_all_hook_types_parseable(self) -> None:
        raw = {
            "pre-install": [
                {"type": "command", "run": "echo 1"},
                {"type": "webhook", "url": "https://h.com"},
                {"type": "script", "path": ".apm/hooks/pre.sh"},
            ],
        }
        hooks = parse_hooks_from_config(raw)
        types = {h.hook_type for h in hooks}
        assert types == set(HOOK_TYPES)


# -- collect_hooks ----------------------------------------------------------


class TestCollectHooks:
    def test_merges_all_three_levels(self) -> None:
        policy = {"post-install": [{"type": "webhook", "url": "https://policy.com"}]}
        global_ = {"post-install": [{"type": "command", "run": "echo global"}]}
        project = {"post-install": [{"type": "script", "path": ".apm/hooks/p.sh"}]}

        hooks = collect_hooks(
            project_hooks_raw=project,
            global_hooks_raw=global_,
            policy_hooks_raw=policy,
        )
        assert len(hooks) == 3
        # Policy first, then global, then project.
        assert hooks[0].source == "policy"
        assert hooks[1].source == "global"
        assert hooks[2].source == "project"

    def test_deduplicates_by_identity_key(self) -> None:
        same_hook = {"post-install": [{"type": "command", "run": "echo same"}]}
        hooks = collect_hooks(
            project_hooks_raw=same_hook,
            global_hooks_raw=same_hook,
        )
        # Same (event, type, run) -- should keep only the first (global wins).
        assert len(hooks) == 1
        assert hooks[0].source == "global"

    def test_policy_cannot_be_overridden_by_project(self) -> None:
        same = {"post-install": [{"type": "webhook", "url": "https://org.com"}]}
        hooks = collect_hooks(policy_hooks_raw=same, project_hooks_raw=same)
        assert len(hooks) == 1
        assert hooks[0].source == "policy"

    def test_none_inputs_produce_empty(self) -> None:
        assert collect_hooks() == []

    def test_different_events_not_deduplicated(self) -> None:
        pre = {"pre-install": [{"type": "command", "run": "echo x"}]}
        post = {"post-install": [{"type": "command", "run": "echo x"}]}
        hooks = collect_hooks(project_hooks_raw={**pre, **post})
        assert len(hooks) == 2


# -- LifecycleHookRunner ---------------------------------------------------


class TestLifecycleHookRunner:
    def _make_event(self, event_name: str = "post-install") -> LifecycleEvent:
        return LifecycleEvent(
            event=event_name,
            packages=[PackageInfo(name="org/repo")],
            scope="project",
            timestamp="2026-01-01T00:00:00Z",
            cli_version="0.0.0",
        )

    def test_fire_calls_matching_hooks(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="echo hi")
        runner = LifecycleHookRunner(hooks=[hook])
        with patch("apm_cli.core.hook_executors.execute_hook") as mock_exec:
            runner.fire("post-install", self._make_event())
            mock_exec.assert_called_once()

    def test_fire_skips_non_matching_events(self) -> None:
        hook = HookDefinition(hook_type="command", event="pre-install", run="echo")
        runner = LifecycleHookRunner(hooks=[hook])
        with patch("apm_cli.core.hook_executors.execute_hook") as mock_exec:
            runner.fire("post-install", self._make_event())
            mock_exec.assert_not_called()

    def test_error_isolation_one_failing_hook_does_not_block_others(self) -> None:
        hook1 = HookDefinition(hook_type="command", event="post-install", run="fail")
        hook2 = HookDefinition(hook_type="command", event="post-install", run="ok")
        runner = LifecycleHookRunner(hooks=[hook1, hook2])
        call_count = 0

        def _side_effect(hook, event, **kw):
            nonlocal call_count
            call_count += 1
            if hook.run == "fail":
                raise RuntimeError("boom")

        with patch("apm_cli.core.hook_executors.execute_hook", side_effect=_side_effect):
            runner.fire("post-install", self._make_event())
        assert call_count == 2  # both hooks were attempted

    def test_fire_with_no_hooks_is_noop(self) -> None:
        runner = LifecycleHookRunner(hooks=[])
        # Should not raise.
        runner.fire("post-install", self._make_event())

    def test_verbose_logs_on_failure(self) -> None:
        hook = HookDefinition(hook_type="command", event="post-install", run="bad")
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
        assert set(HOOK_TYPES) == {"command", "webhook", "script"}
