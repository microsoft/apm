"""Regression tests for additive ``--skill`` *deployment* parity (issue #1786).

PR #1786 made ``apm.yml``/lockfile persistence of ``--skill`` subsets additive,
but the deployment path kept using the raw CLI subset, so a second
``apm install <bundle> --skill B`` deployed only ``B`` and the stale-file
cleanup deleted the previously deployed skill ``A`` from the target folder.

These tests pin the additive *deployment* contract:

* unit: ``effective_deploy_skill_subset`` union semantics + ``--skill '*'`` reset
* e2e:  two atomic ``--skill`` installs leave BOTH skills on disk and in the
        lockfile; ``--skill '*'`` deploys all; a manifest-driven reduction
        cleans the dropped skill; uninstall removes the whole union.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apm_cli.install.package_resolution import (
    apply_cli_skill_pin,
    cli_skill_subset,
    effective_dependency_selection,
    effective_deploy_skill_subset,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Unit: the additive-union helper
# ---------------------------------------------------------------------------


class TestEffectiveDeploySkillSubset:
    def test_bare_install_uses_persisted_union(self):
        # No CLI --skill (bare reinstall): deploy whatever the manifest pins.
        result = effective_deploy_skill_subset(
            skill_subset_from_cli=False,
            cli_subset=None,
            persisted_subset=["alpha", "beta"],
        )
        assert result == ("alpha", "beta")

    def test_bare_install_no_subset_returns_none(self):
        result = effective_deploy_skill_subset(
            skill_subset_from_cli=False,
            cli_subset=None,
            persisted_subset=None,
        )
        assert result is None

    def test_additive_cli_unions_with_persisted(self):
        # `apm install bundle --skill beta` when manifest already pins alpha:
        # deploy BOTH (this is the bug the fix closes).
        result = effective_deploy_skill_subset(
            skill_subset_from_cli=True,
            cli_subset=("beta",),
            persisted_subset=["alpha", "beta"],
        )
        assert result == ("alpha", "beta")

    def test_additive_cli_includes_requested_even_if_manifest_stale(self):
        # Safety net: the just-requested CLI skill is always deployed even if
        # the persisted subset has not yet caught up.
        result = effective_deploy_skill_subset(
            skill_subset_from_cli=True,
            cli_subset=("gamma",),
            persisted_subset=["alpha"],
        )
        assert result == ("alpha", "gamma")

    def test_star_reset_returns_none(self):
        # `--skill '*'` => from_cli True with empty CLI subset => deploy all.
        result = effective_deploy_skill_subset(
            skill_subset_from_cli=True,
            cli_subset=None,
            persisted_subset=["alpha", "beta"],
        )
        assert result is None

    def test_result_is_sorted_and_deduped(self):
        result = effective_deploy_skill_subset(
            skill_subset_from_cli=True,
            cli_subset=("beta", "beta"),
            persisted_subset=["alpha", "beta"],
        )
        assert result == ("alpha", "beta")


# ---------------------------------------------------------------------------
# Unit: the CLI-subset normalizer + skill-pin applier
# ---------------------------------------------------------------------------


class TestEffectiveDependencySelection:
    """The single deploy-selection owner shared by install, audit and uninstall (#3760)."""

    def test_without_install_context_returns_persisted_selection(self):
        dep_ref = SimpleNamespace(skill_subset=["beta", "alpha"], target_subset=["claude"])
        selection = effective_dependency_selection(dep_ref)
        assert selection == (("alpha", "beta"), ["claude"], None)

    def test_install_context_adds_cli_overlay(self):
        dep_ref = SimpleNamespace(skill_subset=["alpha"], target_subset=None)
        ctx = SimpleNamespace(skill_subset_from_cli=True, skill_subset=("beta",))
        assert effective_dependency_selection(dep_ref, ctx) == (("alpha", "beta"), None, None)

    def test_lockfile_records_the_selection_the_owner_deploys(self):
        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.install.phases.lockfile import LockfileBuilder

        bundle = LockedDependency(
            repo_url="owner/bundle", package_type="skill_bundle", skill_subset=["alpha"]
        )
        other = LockedDependency(
            repo_url="owner/other", package_type="apm_package", skill_subset=["keep"]
        )
        ctx = SimpleNamespace(skill_subset_from_cli=True, skill_subset=("beta",))
        lockfile = SimpleNamespace(dependencies={"bundle": bundle, "other": other})
        LockfileBuilder(ctx)._attach_skill_subset_override(lockfile)
        assert bundle.skill_subset == list(effective_dependency_selection(bundle, ctx)[0])
        assert bundle.skill_subset == ["alpha", "beta"]
        assert other.skill_subset == ["keep"]

    def test_implicit_local_dependency_ignores_cli_overlay(self):
        """A partial install's implicit local dep keeps its persisted selection (#4198)."""
        dep_ref = SimpleNamespace(
            skill_subset=["review"], target_subset=None, get_unique_key=lambda: "./kit"
        )
        ctx = SimpleNamespace(
            skill_subset_from_cli=True,
            skill_subset=("alpha",),
            implicit_local_keys=frozenset({"./kit"}),
        )
        assert effective_dependency_selection(dep_ref, ctx) == (("review",), None, None)

    def test_named_dependency_still_gets_cli_overlay_beside_implicit_locals(self):
        dep_ref = SimpleNamespace(
            skill_subset=None, target_subset=None, get_unique_key=lambda: "owner/bundle"
        )
        ctx = SimpleNamespace(
            skill_subset_from_cli=True,
            skill_subset=("alpha",),
            implicit_local_keys=frozenset({"./kit"}),
        )
        assert effective_dependency_selection(dep_ref, ctx) == (("alpha",), None, None)

    def test_first_party_content_has_no_selection(self):
        ctx = SimpleNamespace(skill_subset_from_cli=True, skill_subset=("beta",))
        assert effective_dependency_selection(None, ctx) == (None, None, None)


class TestCliSkillSubset:
    def test_absent_returns_none(self):
        assert cli_skill_subset(()) is None

    def test_star_returns_none(self):
        # `--skill '*'` collapses to None (all skills); origin tracked separately.
        assert cli_skill_subset(("*",)) is None

    def test_named_returns_tuple(self):
        assert cli_skill_subset(("beta",)) == ("beta",)

    def test_star_mixed_with_names_still_resets(self):
        # Any '*' present means "all skills", even alongside named skills.
        assert cli_skill_subset(("alpha", "*")) is None


class _FakeDepRef:
    """Minimal stand-in exercising apply_cli_skill_pin's reset/no-op branches."""

    def __init__(self, identity, canonical, yml_entry):
        self._identity = identity
        self._canonical = canonical
        self._yml_entry = yml_entry
        self.skill_subset = ["stale"]

    def get_identity(self):
        return self._identity

    def to_canonical(self):
        return self._canonical

    def to_apm_yml_entry(self):
        return self._yml_entry


class TestApplyCliSkillPin:
    def test_star_reset_clears_pin_and_records_full_entry(self):
        # `--skill '*'`: clear the pin and write the refreshed full-bundle entry.
        dep_ref = _FakeDepRef("owner/bundle", "owner/bundle@main", "owner/bundle")
        entries: dict = {}
        apply_cli_skill_pin(
            dep_ref,
            None,
            True,
            [],
            entries,
            dependency_reference_cls=object,
        )
        assert dep_ref.skill_subset is None
        assert entries == {"owner/bundle@main": "owner/bundle"}

    def test_no_cli_flag_is_noop(self):
        # Bare install (flag absent): leave the pin and apm.yml entries untouched.
        dep_ref = _FakeDepRef("owner/bundle", "owner/bundle@main", "owner/bundle")
        entries: dict = {}
        apply_cli_skill_pin(
            dep_ref,
            None,
            False,
            [],
            entries,
            dependency_reference_cls=object,
        )
        assert dep_ref.skill_subset == ["stale"]
        assert entries == {}


class TestUnavailableSkillRetryHint:
    """The --skill recovery command repeats the failed install's scope and target."""

    def test_retry_hint_keeps_global_scope_and_explicit_target(self):
        from apm_cli.install.outcome import require_requested_components

        diagnostics = MagicMock()
        ok = require_requested_components(
            diagnostics,
            option="--skill",
            component="skill",
            requested=("nope",),
            available=("alpha",),
            package="owner/bundle",
            install_spec="owner/bundle",
            retry_args=("--target", "claude", "--global"),
        )
        assert ok is False
        message = diagnostics.error.call_args.args[0]
        assert "Run 'apm install owner/bundle --skill alpha --target claude --global'" in message
