"""Unit tests for the heal-pipeline dispatcher
(``apm_cli.install.phases.heal.run_heal_chain``).

Focus: dispatch invariants -- ordering, exclusive_group short-circuit,
no-heal passthrough, message rendering, bypass-key propagation -- using
synthetic heal classes so the test does not depend on the production
heals beyond confirming the dispatcher contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.install.heals.base import HealContext, HealMessage, HealMessageLevel
from apm_cli.install.phases.heal import run_heal_chain


class _FakeHeal:
    def __init__(
        self,
        *,
        name: str,
        order: int,
        exclusive_group: str | None,
        applies_result: bool,
        emits: HealMessageLevel | None = None,
        adds_bypass: bool = True,
        sets_lockfile_match_false: bool = True,
        sets_ref_changed_true: bool = True,
    ):
        self.name = name
        self.order = order
        self.exclusive_group = exclusive_group
        self._applies_result = applies_result
        self._emits = emits
        self._adds_bypass = adds_bypass
        self._sets_lockfile_match_false = sets_lockfile_match_false
        self._sets_ref_changed_true = sets_ref_changed_true
        self.applies_calls = 0
        self.execute_calls = 0

    def applies(self, hctx):
        self.applies_calls += 1
        return self._applies_result

    def execute(self, hctx):
        self.execute_calls += 1
        if self._sets_lockfile_match_false:
            hctx.lockfile_match = False
        if self._sets_ref_changed_true:
            hctx.ref_changed = True
        if self._adds_bypass:
            hctx.add_bypass_key(hctx.package_key)
        if self._emits is not None:
            hctx.emit(self._emits, f"{self.name}-fired")


def _make_ctx_and_dep(*, package_key: str = "github.com/owner/repo"):
    ctx = MagicMock()
    ctx.expected_hash_change_deps = set()
    ctx.diagnostics = MagicMock()
    ctx.logger = MagicMock()

    dep_ref = MagicMock()
    dep_ref.get_unique_key.return_value = package_key
    return ctx, dep_ref


def _kwargs(**overrides):
    base = dict(
        resolved_ref=MagicMock(),
        existing_lockfile=MagicMock(),
        lockfile_match=True,
        lockfile_match_via_content_hash_only=False,
        update_refs=False,
        ref_changed=False,
    )
    base.update(overrides)
    return base


class TestNoHealsFire:
    def test_passthrough_returns_inputs_unchanged(self):
        ctx, dep_ref = _make_ctx_and_dep()
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", ()):
            result = run_heal_chain(ctx, dep_ref, **_kwargs(lockfile_match=True, ref_changed=False))
        assert result == (True, False)
        assert ctx.expected_hash_change_deps == set()
        ctx.diagnostics.warn.assert_not_called()


class TestSingleHealFires:
    def test_info_emit_routes_to_verbose_only(self):
        ctx, dep_ref = _make_ctx_and_dep()
        h = _FakeHeal(
            name="info_heal",
            order=10,
            exclusive_group=None,
            applies_result=True,
            emits=HealMessageLevel.INFO,
        )
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", (h,)):
            lockfile_match, ref_changed = run_heal_chain(ctx, dep_ref, **_kwargs())
        assert (lockfile_match, ref_changed) == (False, True)
        ctx.logger.verbose_detail.assert_called_once()
        ctx.diagnostics.warn.assert_not_called()
        ctx.logger.progress.assert_not_called()
        assert "github.com/owner/repo" in ctx.expected_hash_change_deps

    def test_warn_emit_routes_to_diagnostics_and_progress(self):
        ctx, dep_ref = _make_ctx_and_dep()
        h = _FakeHeal(
            name="warn_heal",
            order=10,
            exclusive_group=None,
            applies_result=True,
            emits=HealMessageLevel.WARN,
        )
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", (h,)):
            run_heal_chain(ctx, dep_ref, **_kwargs())
        ctx.diagnostics.warn.assert_called_once()
        ctx.logger.progress.assert_called_once()
        ctx.logger.verbose_detail.assert_not_called()


class TestExclusiveGroup:
    def test_first_in_group_short_circuits_later(self):
        ctx, dep_ref = _make_ctx_and_dep()
        h1 = _FakeHeal(
            name="first",
            order=10,
            exclusive_group="grp",
            applies_result=True,
            emits=HealMessageLevel.INFO,
        )
        h2 = _FakeHeal(
            name="second",
            order=20,
            exclusive_group="grp",
            applies_result=True,
            emits=HealMessageLevel.WARN,
        )
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", (h1, h2)):
            run_heal_chain(ctx, dep_ref, **_kwargs())
        assert h1.execute_calls == 1
        # Second heal in same group must be skipped without even calling .applies()
        assert h2.applies_calls == 0
        assert h2.execute_calls == 0
        # Only the INFO from h1 was rendered
        ctx.diagnostics.warn.assert_not_called()

    def test_unrelated_groups_both_fire(self):
        ctx, dep_ref = _make_ctx_and_dep()
        h1 = _FakeHeal(
            name="first",
            order=10,
            exclusive_group="grp_a",
            applies_result=True,
            emits=HealMessageLevel.INFO,
        )
        h2 = _FakeHeal(
            name="second",
            order=20,
            exclusive_group="grp_b",
            applies_result=True,
            emits=HealMessageLevel.WARN,
        )
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", (h1, h2)):
            run_heal_chain(ctx, dep_ref, **_kwargs())
        assert h1.execute_calls == 1
        assert h2.execute_calls == 1

    def test_no_group_means_no_short_circuit(self):
        ctx, dep_ref = _make_ctx_and_dep()
        h1 = _FakeHeal(
            name="first", order=10, exclusive_group=None, applies_result=True, emits=None
        )
        h2 = _FakeHeal(
            name="second", order=20, exclusive_group=None, applies_result=True, emits=None
        )
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", (h1, h2)):
            run_heal_chain(ctx, dep_ref, **_kwargs())
        assert h1.execute_calls == 1
        assert h2.execute_calls == 1


class TestApplicabilityFilter:
    def test_does_not_execute_when_applies_returns_false(self):
        ctx, dep_ref = _make_ctx_and_dep()
        h = _FakeHeal(name="skip", order=10, exclusive_group=None, applies_result=False, emits=None)
        with patch("apm_cli.install.phases.heal.HEAL_CHAIN", (h,)):
            lockfile_match, ref_changed = run_heal_chain(
                ctx, dep_ref, **_kwargs(lockfile_match=True, ref_changed=False)
            )
        assert h.applies_calls == 1
        assert h.execute_calls == 0
        assert (lockfile_match, ref_changed) == (True, False)


class TestHealContextStructure:
    def test_emit_helper_attaches_package_key(self):
        ctx = HealContext(
            dep_ref=MagicMock(),
            package_key="dummy.example.com/x",
            resolved_ref=None,
            existing_lockfile=None,
            lockfile_match=True,
            lockfile_match_via_content_hash_only=False,
            update_refs=False,
        )
        ctx.emit(HealMessageLevel.WARN, "hello")
        assert ctx.messages == [
            HealMessage(
                level=HealMessageLevel.WARN, text="hello", package_key="dummy.example.com/x"
            )
        ]
