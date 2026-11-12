"""Component tests for ``HookIntegrator.reconcile_dropped_targets``.

Regression coverage for issue #2253: narrowing a project's declared
``targets:`` list never reconciled the dropped target's APM-owned
merge-hook JSON config + ``apm-hooks.json`` ownership sidecar, even after
``apm install`` + ``apm prune`` -- ``_clean_apm_entries_from_json`` (the
existing, UNCHANGED primitive used by ``sync_integration``/
``reconcile_after_removal``) early-returns with no sidecar handling at all
when the native config file is absent, so a sidecar-only orphan is never
seen by ANY existing caller.

These tests exercise ``HookIntegrator.reconcile_dropped_targets`` directly
against hand-built fixtures (not through the CLI) because the partial/
malformed-state edge cases here are not reachable through a real ``apm
install`` for any target currently registered in ``_MERGE_HOOK_TARGETS``:
every registered target defaults ``schema_strict=True`` (ownership always
lives in the sidecar, never inline in native JSON -- see
``_MergeHookConfig``), so "native carries the marker with no sidecar" can
only arise from hand-authored or historical/malformed state, which these
fixtures construct directly. Full end-to-end lifecycle proof (narrow +
install + prune, negative twins, union-rule, dry-run, compile parity) lives
in the integration-contract file ``tests/integration/
test_hook_target_contraction_reconciliation.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.integration.hook_integrator import HookIntegrator


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestReconcileDroppedTargetsNormalCleanup:
    def test_codex_native_and_sidecar_cleaned_together(self, tmp_path: Path) -> None:
        """The real-world shape for every current merge-hook target:
        native carries no marker (schema-strict), sidecar records ownership.
        The native entry's non-marker content must match the sidecar's
        recorded entry (minus ``_apm_source``) for the shared
        ``_clean_apm_entries_from_json`` primitive to re-associate and strip
        it -- content-keyed re-injection, not raw event-name presence."""
        codex_dir = tmp_path / ".codex"
        _write_json(
            codex_dir / "hooks.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo x"}]}
                    ]
                }
            },
        )
        _write_json(
            codex_dir / "apm-hooks.json",
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo x"}],
                        "_apm_source": "fixture-claude-codex-hook-narrow",
                    }
                ]
            },
        )

        stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])

        assert stats["errors"] == 0
        assert stats["files_removed"] >= 1
        assert not (codex_dir / "apm-hooks.json").exists()
        native_data = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        assert native_data.get("hooks", {}) == {}, "package-owned entry must be stripped"

    def test_unknown_target_name_is_silently_skipped(self, tmp_path: Path) -> None:
        """A name not in _MERGE_HOOK_TARGETS (e.g. copilot) is a no-op here --
        it is already correctly cleaned via the generic deployed_files path."""
        stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["copilot", "kiro"])
        assert stats == {"files_removed": 0, "errors": 0}

    def test_no_state_on_disk_is_a_clean_noop(self, tmp_path: Path) -> None:
        stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])
        assert stats == {"files_removed": 0, "errors": 0}

    def test_reconciliation_is_idempotent(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        _write_json(
            codex_dir / "hooks.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo x"}]}
                    ]
                }
            },
        )
        _write_json(
            codex_dir / "apm-hooks.json",
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo x"}],
                        "_apm_source": "pkg",
                    }
                ]
            },
        )
        integrator = HookIntegrator()
        first = integrator.reconcile_dropped_targets(tmp_path, ["codex"])
        assert first["files_removed"] >= 1
        second = integrator.reconcile_dropped_targets(tmp_path, ["codex"])
        assert second == {"files_removed": 0, "errors": 0}


class TestReconcileDroppedTargetsCursor:
    """Required-gate node-2 evidence: Cursor is registered in
    ``_MERGE_HOOK_TARGETS`` with the same ``schema_strict=True`` default as
    Codex/Claude, so it must be cleaned by the SAME generic code path --
    not a Codex-specific one. Mirrors the exact evidence shape reported
    against a real widen-then-narrow (Claude -> Claude+Cursor -> Claude)
    CLI run: a package-owned ``PreToolUse`` entry in ``.cursor/hooks.json``,
    a user-authored sibling entry, and an ``apm-hooks.json`` sidecar
    recording ``_apm_source``. Direct end-to-end CLI coverage of this same
    scenario lives in ``tests/integration/
    test_hook_target_contraction_reconciliation.py::
    test_widen_then_narrow_removes_dropped_cursor_hook_state`` and
    ``test_widen_then_narrow_preserves_user_owned_cursor_entries``."""

    def test_cursor_native_and_sidecar_cleaned_preserving_user_entry(self, tmp_path: Path) -> None:
        cursor_dir = tmp_path / ".cursor"
        _write_json(
            cursor_dir / "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo scope"}],
                        },
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo user-authored"}],
                        },
                    ]
                },
            },
        )
        _write_json(
            cursor_dir / "apm-hooks.json",
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo scope"}],
                        "_apm_source": "scope-kit",
                    }
                ]
            },
        )
        # Retained target's state, alongside the dropped target -- must
        # survive completely untouched.
        claude_dir = tmp_path / ".claude"
        _write_json(
            claude_dir / "settings.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo scope"}]}
                    ]
                }
            },
        )
        _write_json(
            claude_dir / "apm-hooks.json",
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo scope"}],
                        "_apm_source": "scope-kit",
                    }
                ]
            },
        )

        stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["cursor"])

        assert stats == {"files_removed": 1, "errors": 0}
        assert not (cursor_dir / "apm-hooks.json").exists(), (
            "dropped cursor target's ownership sidecar must be removed"
        )
        native_data = json.loads((cursor_dir / "hooks.json").read_text(encoding="utf-8"))
        remaining_commands = [
            handler["command"]
            for entry in native_data.get("hooks", {}).get("PreToolUse", [])
            for handler in entry.get("hooks", [])
        ]
        assert "echo scope" not in remaining_commands, (
            "package-owned cursor entry must be stripped from the native merged config"
        )
        assert "echo user-authored" in remaining_commands, (
            "user-authored cursor entry must survive untouched"
        )
        assert (claude_dir / "apm-hooks.json").exists(), (
            "retained claude target's ownership sidecar must not be touched"
        )
        claude_sidecar = json.loads((claude_dir / "apm-hooks.json").read_text(encoding="utf-8"))
        assert claude_sidecar["PreToolUse"][0]["_apm_source"] == "scope-kit", (
            "retained claude target's ownership record must be byte-for-byte preserved"
        )


class TestReconcileDroppedTargetsSidecarOnlyOrphan:
    def test_sidecar_only_orphan_is_removed(self, tmp_path: Path) -> None:
        """Native file absent (manually deleted, or never re-created) but the
        ownership sidecar remains -- the exact gap _clean_apm_entries_from_json
        cannot see (its own first line returns before ever inspecting a
        sidecar when the native path does not exist)."""
        sidecar = tmp_path / ".codex" / "apm-hooks.json"
        _write_json(sidecar, {"PreToolUse": [{"_apm_source": "pkg"}]})

        stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])

        assert stats == {"files_removed": 1, "errors": 0}
        assert not sidecar.exists()

    def test_malformed_sidecar_only_orphan_fails_closed(self, tmp_path: Path, caplog) -> None:
        sidecar = tmp_path / ".codex" / "apm-hooks.json"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("{not valid json", encoding="utf-8")
        before = sidecar.read_text(encoding="utf-8")

        with caplog.at_level("WARNING"):
            stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])

        assert stats == {"files_removed": 0, "errors": 1}
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == before, (
            "malformed sidecar left byte-identical"
        )
        assert any(
            "malformed" in record.message.lower() or "unreadable" in record.message.lower()
            for record in caplog.records
        )


class TestReconcileDroppedTargetsMalformedNative:
    def test_malformed_native_json_fails_closed_with_warning(self, tmp_path: Path, caplog) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir(parents=True)
        native = codex_dir / "hooks.json"
        native.write_text("{not valid json", encoding="utf-8")
        before = native.read_text(encoding="utf-8")
        sidecar = codex_dir / "apm-hooks.json"
        _write_json(sidecar, {"PreToolUse": [{"_apm_source": "pkg"}]})

        with caplog.at_level("WARNING"):
            stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])

        assert stats["errors"] == 1
        assert native.read_text(encoding="utf-8") == before, "malformed native left byte-identical"
        assert any(
            "malformed" in record.message.lower() or "unreadable" in record.message.lower()
            for record in caplog.records
        )

    def test_native_marker_stripped_when_present_without_sidecar(self, tmp_path: Path) -> None:
        """Defense-in-depth: if a native file ever carries an inline
        _apm_source marker with no sidecar present (hand-authored/legacy
        state -- no currently-registered target produces this via a real
        install, since every target defaults schema_strict=True), the
        shared _clean_apm_entries_from_json primitive still strips it."""
        native = tmp_path / ".codex" / "hooks.json"
        _write_json(
            native,
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo x"}],
                            "_apm_source": "pkg",
                        }
                    ]
                }
            },
        )

        stats = HookIntegrator().reconcile_dropped_targets(tmp_path, ["codex"])

        assert stats == {"files_removed": 1, "errors": 0}
        data = json.loads(native.read_text(encoding="utf-8"))
        assert data.get("hooks", {}) == {}
