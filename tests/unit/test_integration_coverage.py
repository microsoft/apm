"""Unit tests for apm_cli.integration.coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.integration.coverage import check_primitive_coverage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dispatch(primitives: list[str]) -> dict:
    """Build a minimal dispatch table dict keyed by primitive name."""
    table = {}
    for p in primitives:
        entry = MagicMock()
        entry.integrator_class = type(f"{p.capitalize()}Integrator", (), {})
        entry.integrate_method = None
        entry.sync_method = None
        table[p] = entry
    return table


def _mock_known_targets(monkeypatch, primitives_by_target: dict[str, list[str]]) -> None:
    """Patch KNOWN_TARGETS so coverage tests run in isolation."""
    targets = {}
    for target_name, prims in primitives_by_target.items():
        target = MagicMock()
        target.primitives = {p: object() for p in prims}
        targets[target_name] = target
    monkeypatch.setattr(
        "apm_cli.integration.coverage.apm_cli.integration.targets.KNOWN_TARGETS",
        targets,
        raising=False,
    )
    # Also patch at the direct import path used inside the function
    import apm_cli.integration.targets as _targets_mod

    monkeypatch.setattr(_targets_mod, "KNOWN_TARGETS", targets)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckPrimitiveCoverage:
    """Tests for check_primitive_coverage()."""

    def test_all_handled_no_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every primitive has an entry: no exception."""
        from apm_cli.integration import targets as _t

        original = _t.KNOWN_TARGETS

        # Build a dispatch table that covers all existing primitives
        all_prims: set[str] = set()
        for target in original.values():
            all_prims.update(target.primitives.keys())

        dispatch = _make_dispatch(list(all_prims))
        # Should not raise
        check_primitive_coverage(dispatch)

    def test_missing_primitive_raises_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A primitive in KNOWN_TARGETS with no dispatch entry raises RuntimeError."""
        from apm_cli.integration import targets as _t

        all_prims: set[str] = set()
        for target in _t.KNOWN_TARGETS.values():
            all_prims.update(target.primitives.keys())

        # Remove one primitive from the dispatch table
        prims_list = sorted(all_prims)
        if not prims_list:
            pytest.skip("No primitives registered; nothing to test.")

        missing = prims_list[0]
        dispatch = _make_dispatch([p for p in prims_list if p != missing])

        with pytest.raises(RuntimeError, match="no integrator in the dispatch table"):
            check_primitive_coverage(dispatch)

    def test_extra_dispatch_entry_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dispatch entry for a non-existent primitive raises RuntimeError."""
        from apm_cli.integration import targets as _t

        all_prims: set[str] = set()
        for target in _t.KNOWN_TARGETS.values():
            all_prims.update(target.primitives.keys())

        dispatch = _make_dispatch(list(all_prims))
        # Add a stale entry
        stale = MagicMock()
        stale.integrator_class = type("StaleIntegrator", (), {})
        stale.integrate_method = None
        stale.sync_method = None
        dispatch["__nonexistent_primitive__"] = stale

        with pytest.raises(RuntimeError, match="not present in"):
            check_primitive_coverage(dispatch)

    def test_special_cases_excluded_from_missing_check(self) -> None:
        """Primitives in special_cases are exempt from the dispatch-table check."""
        from apm_cli.integration import targets as _t

        all_prims: set[str] = set()
        for target in _t.KNOWN_TARGETS.values():
            all_prims.update(target.primitives.keys())

        if not all_prims:
            pytest.skip("No primitives registered.")

        # Move one primitive into special_cases, leave it out of dispatch
        special = sorted(all_prims)[0]
        dispatch = _make_dispatch([p for p in all_prims if p != special])
        # Should not raise
        check_primitive_coverage(dispatch, special_cases={special})

    def test_method_existence_check_passes_for_valid_method(self) -> None:
        """Dispatch entry with valid method names on integrator_class passes."""
        from apm_cli.integration import targets as _t

        all_prims: set[str] = set()
        for target in _t.KNOWN_TARGETS.values():
            all_prims.update(target.primitives.keys())

        dispatch: dict = {}
        for p in all_prims:
            entry = MagicMock()
            # Give a real class with real methods
            entry.integrator_class = type("I", (), {"do_integrate": lambda self: None})
            entry.integrate_method = "do_integrate"
            entry.sync_method = None
            dispatch[p] = entry

        check_primitive_coverage(dispatch)  # should not raise

    def test_method_existence_check_raises_for_missing_method(self) -> None:
        """Dispatch entry referencing a missing method name raises RuntimeError."""
        from apm_cli.integration import targets as _t

        all_prims: set[str] = set()
        for target in _t.KNOWN_TARGETS.values():
            all_prims.update(target.primitives.keys())

        if not all_prims:
            pytest.skip("No primitives registered.")

        prim = sorted(all_prims)[0]
        dispatch = _make_dispatch(list(all_prims))
        # Override prim to reference a nonexistent method
        entry = MagicMock()
        entry.integrator_class = type("I", (), {})
        entry.integrate_method = "nonexistent_method"
        entry.sync_method = None
        dispatch[prim] = entry

        with pytest.raises(RuntimeError, match="missing method"):
            check_primitive_coverage(dispatch)

    def test_none_special_cases_defaults_to_empty_set(self) -> None:
        """Passing special_cases=None is equivalent to special_cases=set()."""
        from apm_cli.integration import targets as _t

        all_prims: set[str] = set()
        for target in _t.KNOWN_TARGETS.values():
            all_prims.update(target.primitives.keys())

        dispatch = _make_dispatch(list(all_prims))
        # Should not raise
        check_primitive_coverage(dispatch, special_cases=None)

    def test_empty_dispatch_empty_targets_no_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When KNOWN_TARGETS is empty and dispatch is empty, no error."""
        import apm_cli.integration.targets as _t

        monkeypatch.setattr(_t, "KNOWN_TARGETS", {})
        check_primitive_coverage({})
