"""Cursor-native hook installation conformance -- sec.8.5.9.

req-tg-016 and req-tg-017 are the spec-citation fold for PR #3149's
already-shipped Cursor-native hook capability: fail-closed conversion
validation and Claude-import-coexistence rejection. These are
silent-deletion detectors that pin the normative phrasing in place;
the actual behavioral proof is the already-existing, already-passing
integration suite named in the editorial note below (not duplicated
here), consistent with this directory's drift-sentinel pattern for
requirements backed by a real shipped implementation rather than a
schema or fixture.
"""

from __future__ import annotations

import pytest

from tests.spec_conformance._helpers import assert_spec_contains


@pytest.mark.req("req-tg-016")
def test_cursor_native_fail_closed_conversion_clause_persists_in_spec() -> None:
    """Silent-deletion detector for the req-tg-016 normative clause.

    Behavioral proof (not duplicated here):
    tests/unit/integration/test_cursor_hook_native_contract.py::
    test_unrepresentable_hooks_fail_without_native_or_script_writes,
    ::test_cursor_install_emits_native_events_and_flat_handlers.
    """
    assert_spec_contains(
        "MUST fail closed when converting a source-declared hook\n"
        "configuration into that Cursor-native format",
        "the implementation MUST NOT write the Cursor-native hook\n"
        "artifact (zero bytes, no partial file)",
        "MUST emit an actionable diagnostic\nnaming the unrecognized value(s)",
    )


@pytest.mark.req("req-tg-017")
def test_cursor_claude_import_overlap_rejection_clause_persists_in_spec() -> None:
    """Silent-deletion detector for the req-tg-017 normative clause.

    Behavioral proof (not duplicated here):
    tests/unit/integration/test_cursor_hook_native_contract.py::
    test_import_overlap_refused_in_either_install_order,
    ::test_import_locations_preserved_and_overlap_rejected,
    ::test_unapproved_hooks_do_not_enter_cursor_preflight;
    tests/integration/test_package_target_hook_routing_e2e.py::
    test_package_target_transition_repairs_cursor_and_uninstall_preserves_user_hook,
    ::test_failed_restricted_update_preserves_existing_hook_state;
    tests/integration/test_cursor_hook_lifecycle.py::
    test_cursor_installed_cli_contract.
    """
    assert_spec_contains(
        "for\nboth install orders (Claude-import-first and Cursor-native-first)",
        "MUST reject the write with an actionable\n"
        "diagnostic rather than merge, redirect, or broaden either side's accepted\n"
        "input",
        "MUST NOT alter the Claude import's own settings",
    )
