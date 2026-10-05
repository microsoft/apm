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

import json
from pathlib import Path

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


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _package(tmp_path: Path, hooks: dict[str, object]):
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    root = tmp_path / "req-tg-fixture"
    _write_json(root / ".apm/hooks/gate.json", {"hooks": hooks})
    script = root / ".apm/hooks/scripts/gate.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('{}')\n", encoding="utf-8")
    return PackageInfo(
        package=APMPackage(name="req-tg-fixture", version="1.0.0"),
        install_path=root,
    )


@pytest.mark.req("req-tg-016")
def test_cursor_native_fail_closed_conversion_is_real_not_prose(tmp_path: Path) -> None:
    """Real fail-closed behavior proof for req-tg-016, with a mutation control.

    The positive case proves an out-of-vocabulary source event is rejected
    before any native artifact is written. The mutation control removes the
    fail-closed guard (by widening the accepted event vocabulary, the same
    class of regression a reordered/removed validation check would cause)
    and proves the write would otherwise silently succeed -- demonstrating
    the positive assertion is load-bearing, not an accidental pass.
    """
    from apm_cli.hook_contract import HookContractError
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    package = _package(tmp_path, {"Notification": [{"hooks": [{"command": "echo bad"}]}]})
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)

    with pytest.raises(HookContractError, match="unsupported Cursor event"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert list((project / ".cursor").iterdir()) == [], (
        "a rejected conversion MUST NOT write the Cursor-native hook artifact"
    )

    from apm_cli.integration import hook_integrator as integrator_mod

    cursor_event_map = integrator_mod._HOOK_EVENT_MAP["cursor"]
    assert "Notification" not in cursor_event_map

    cursor_event_map["Notification"] = "preToolUse"
    try:
        widened_project = tmp_path / "project-widened"
        (widened_project / ".cursor").mkdir(parents=True)
        package_widened = _package(
            tmp_path / "widened-src", {"Notification": [{"hooks": [{"command": "echo bad"}]}]}
        )
        HookIntegrator().integrate_hooks_for_target(
            KNOWN_TARGETS["cursor"], package_widened, widened_project
        )
        assert (widened_project / ".cursor/hooks.json").exists(), (
            "mutation control: widening the accepted event vocabulary (the same "
            "class of regression a removed/reordered fail-closed check would "
            "cause) MUST let the same out-of-vocabulary source event through, "
            "proving the guard above is the thing actually stopping it"
        )
    finally:
        del cursor_event_map["Notification"]

    assert_spec_contains(
        "MUST fail closed when converting a source-declared hook",
        "the implementation MUST NOT write the Cursor-native hook\n"
        "artifact (zero bytes, no partial file)",
    )


@pytest.mark.req("req-tg-017")
def test_cursor_claude_overlap_predicate_is_kind_aware_not_event_only(tmp_path: Path) -> None:
    """Real behavior proof for req-tg-017's corrected narrower predicate.

    Positive case: the same event with the same literal text but a
    different handler *kind* (``command`` vs ``prompt``) is NOT an
    overlap under the corrected (event, kind, content) predicate.
    Mutation control: patching the predicate back to the stale
    event-and-content-only (kind-blind) comparison this spec clause used
    to describe MUST turn that same scenario into a rejection, proving
    the positive case depends on kind-awareness rather than passing by
    coincidence.
    """
    from apm_cli.hook_contract import HookContractError
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    package = _package(
        tmp_path, {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo shared"}]}]}
    )
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    claude = project / ".claude/settings.json"
    _write_json(claude, {"hooks": {"PreToolUse": [{"type": "prompt", "prompt": "echo shared"}]}})
    before = claude.read_bytes()

    result = HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert result.files_integrated == 1
    assert claude.read_bytes() == before
    assert (project / ".cursor/hooks.json").exists()

    import apm_cli.integration.hook_cursor_preflight as preflight_mod

    real_action_keys = preflight_mod._action_keys

    def _kind_blind_action_keys(document, event_map, roots, owners, *, owned=False):
        keys = real_action_keys(document, event_map, roots, owners, owned=owned)
        return {(event, "command", content) for event, _kind, content in keys}

    preflight_mod._action_keys = _kind_blind_action_keys
    try:
        package2 = _package(
            tmp_path / "kind-blind-src",
            {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo shared"}]}]},
        )
        project2 = tmp_path / "project-kind-blind"
        (project2 / ".cursor").mkdir(parents=True)
        _write_json(
            project2 / ".claude/settings.json",
            {"hooks": {"PreToolUse": [{"type": "prompt", "prompt": "echo shared"}]}},
        )
        with pytest.raises(HookContractError, match="Claude import"):
            HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package2, project2)
    finally:
        preflight_mod._action_keys = real_action_keys

    assert_spec_contains(
        "a non-empty\nintersection, after alias normalization, between the set of (event\n"
        "identifier, handler kind, handler content) tuples",
        "two entries for the same event but a\ndifferent handler kind or different handler "
        "content are not an overlap",
    )


@pytest.mark.req("req-tg-017")
def test_cursor_claude_import_rejects_real_default_same_event_kind_content_overlap(
    tmp_path: Path,
) -> None:
    """Real default-predicate proof for req-tg-017's rejection branch.

    The test above (``test_cursor_claude_overlap_predicate_is_kind_aware_not_
    event_only``) only reaches a rejection by deliberately patching
    ``_action_keys`` back to a kind-blind mutant; that proves the predicate
    is load-bearing, but not that the unpatched, as-shipped predicate
    actually rejects a genuine overlap. This case uses the real nested
    Claude handler shape (matching ``_nested`` in the native-contract unit
    test) with the SAME event, SAME handler kind ("command"), and SAME
    content as the Cursor source hook -- an unambiguous default-production
    overlap -- and asserts rejection with zero native writes and a
    byte-preserved Claude import, using the unpatched predicate end to end.
    """
    package = _package(
        tmp_path, {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo shared"}]}]}
    )
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    claude = project / ".claude/settings.json"
    _write_json(
        claude,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo shared"}],
                    }
                ]
            }
        },
    )
    before = claude.read_bytes()

    from apm_cli.hook_contract import HookContractError
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    with pytest.raises(HookContractError, match="Claude import"):
        HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], package, project)

    assert claude.read_bytes() == before
    assert list((project / ".cursor").iterdir()) == [], (
        "a rejected Claude-import overlap MUST NOT write the Cursor-native hook artifact"
    )
