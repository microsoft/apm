"""Unit tests for merge-hook target-contraction reconciliation wiring.

Regression trap for the confirmed defect: narrowing apm.yml's `targets:`
list (e.g. `[claude, codex]` -> `[claude]`) never cleaned up the dropped
target's APM-owned merge-hook JSON config + ownership sidecar, even after
`apm install` + `apm prune` (see issue #2253; evidence session
354cbb2d-53da-4290-808f-e8f21e754bcb / PR #2266, evidence-only).

`reconcile_dropped_merge_hook_targets` mirrors `reconcile_deployed_state`'s
own "allowed = active union declared" semantics exactly, including the
`declared_targets is None` legacy preserve-all no-op (#2059 symmetry), and
delegates ALL native-JSON/sidecar mutation to the canonical
`HookIntegrator.reconcile_dropped_targets`. This file asserts only on the
delegation contract (which names were computed as dropped, and that the
call is skipped entirely when it must be); JSON/sidecar content assertions
live in the integration-contract matrix (`tests/integration/
test_hook_target_contraction_reconciliation.py`).
"""

from __future__ import annotations

from apm_cli.install.manifest_reconcile import reconcile_dropped_merge_hook_targets
from apm_cli.integration.targets import KNOWN_TARGETS

_HOOK_INTEGRATOR_RECONCILE_PATH = (
    "apm_cli.integration.hook_integrator.HookIntegrator.reconcile_dropped_targets"
)


def _known(name):
    return KNOWN_TARGETS[name]


class TestReconcileDroppedMergeHookTargetsUnionRule:
    def test_dropped_names_are_known_minus_active_union_declared(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_reconcile(self, project_root, dropped_target_names, *, user_scope=False):
            captured["project_root"] = project_root
            captured["dropped"] = set(dropped_target_names)
            captured["user_scope"] = user_scope
            return {"files_removed": 1, "errors": 0}

        monkeypatch.setattr(_HOOK_INTEGRATOR_RECONCILE_PATH, _fake_reconcile)

        result = reconcile_dropped_merge_hook_targets(
            tmp_path,
            active_targets=[_known("claude")],
            declared_targets=[_known("claude")],
        )

        assert "codex" in captured["dropped"]
        assert "claude" not in captured["dropped"]
        assert captured["project_root"] == tmp_path
        assert captured["user_scope"] is False
        assert result == {"files_removed": 1, "errors": 0}

    def test_active_target_alone_does_not_drop_still_declared_sibling(self, monkeypatch, tmp_path):
        """A transient ``--target claude`` override must not treat codex as
        dropped while apm.yml still declares it (union rule)."""
        captured = {}

        def _fake_reconcile(self, project_root, dropped_target_names, *, user_scope=False):
            captured["dropped"] = set(dropped_target_names)
            return {"files_removed": 0, "errors": 0}

        monkeypatch.setattr(_HOOK_INTEGRATOR_RECONCILE_PATH, _fake_reconcile)

        reconcile_dropped_merge_hook_targets(
            tmp_path,
            active_targets=[_known("claude")],
            declared_targets=[_known("claude"), _known("codex")],
        )

        assert "codex" not in captured["dropped"]

    def test_no_dropped_targets_is_a_noop_and_skips_hook_integrator(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(
            _HOOK_INTEGRATOR_RECONCILE_PATH,
            lambda self, *a, **k: called.append(1),
        )

        all_targets = list(KNOWN_TARGETS.values())
        result = reconcile_dropped_merge_hook_targets(
            tmp_path,
            active_targets=all_targets,
            declared_targets=all_targets,
        )

        assert not called
        assert result == {"files_removed": 0, "errors": 0}

    def test_declared_targets_none_is_a_hard_noop(self, monkeypatch, tmp_path):
        """#2059 symmetry: no declared universe means dropped-target
        detection is impossible -- legacy preserve-all, zero mutation."""
        called = []
        monkeypatch.setattr(
            _HOOK_INTEGRATOR_RECONCILE_PATH,
            lambda self, *a, **k: called.append(1),
        )

        result = reconcile_dropped_merge_hook_targets(
            tmp_path,
            active_targets=[_known("claude")],
            declared_targets=None,
        )

        assert not called
        assert result == {"files_removed": 0, "errors": 0}

    def test_user_scope_is_threaded_through(self, monkeypatch, tmp_path):
        captured = {}

        def _fake_reconcile(self, project_root, dropped_target_names, *, user_scope=False):
            captured["user_scope"] = user_scope
            return {"files_removed": 0, "errors": 0}

        monkeypatch.setattr(_HOOK_INTEGRATOR_RECONCILE_PATH, _fake_reconcile)

        reconcile_dropped_merge_hook_targets(
            tmp_path,
            active_targets=[_known("claude")],
            declared_targets=[_known("claude")],
            user_scope=True,
        )

        assert captured["user_scope"] is True
