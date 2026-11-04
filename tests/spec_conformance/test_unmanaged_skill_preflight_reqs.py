"""Execute installed-CLI evidence for proposed req-pr-008, not prose sentinels."""

from pathlib import Path

import pytest

from tests.integration import test_unmanaged_skill_preflight_lifecycle as lifecycle
from tests.integration import test_unmanaged_skill_preflight_matrix as matrix
from tests.integration.conftest import apm_binary_path as apm_binary_path

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.mark.req("req-pr-008")
def test_known_later_conflict_precedes_complete_plan_writes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Assert actual exit, diagnostic, native bytes, intent and ownership independently."""
    lifecycle.test_later_unmanaged_skill_conflict_refuses_complete_plan_before_native_writes(
        tmp_path, apm_binary_path
    )


@pytest.mark.req("req-pr-008")
@pytest.mark.parametrize("scope", ["project", "user"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("prior_partial", [False, True])
@pytest.mark.parametrize("layout", ["subskills", "bundle", "native"])
def test_selected_scope_order_replay_audit_and_correction(
    tmp_path: Path,
    apm_binary_path: Path,
    scope: str,
    reverse: bool,
    prior_partial: bool,
    layout: str,
) -> None:
    """Exercise repeated/frozen refusal, read-only audit, narrowing, correction and removal."""
    matrix.test_refusal_preserves_plan_then_narrowing_and_correction_converge(
        tmp_path, apm_binary_path, scope, reverse, prior_partial, layout
    )


@pytest.mark.req("req-pr-008")
@pytest.mark.parametrize("reverse", [False, True])
def test_shared_root_refusal_preserves_existing_explicit_force(
    tmp_path: Path, apm_binary_path: Path, reverse: bool
) -> None:
    """Prove the default refusal and explicit destructive choice remain distinct."""
    matrix.test_standalone_skill_refuses_before_earlier_dependency_and_force_is_explicit(
        tmp_path, apm_binary_path, reverse
    )


@pytest.mark.req("req-pr-008")
def test_native_plugin_is_not_a_loose_skill_collision(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An opaque native plugin neither rejects nor claims a same-named personal skill."""
    matrix.test_native_plugin_registration_preserves_same_named_unmanaged_loose_skill(
        tmp_path, apm_binary_path
    )
