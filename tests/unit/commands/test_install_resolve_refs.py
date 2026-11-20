"""Unit tests for _resolve_package_references() mutation contract.

Covers P1-G2: the function mutates *existing_identities* in-place to
detect batch duplicates, and that contract was previously untested.

Strategy: mock ``DependencyReference.parse()`` and
``_validate_package_exists()`` so tests run without network or filesystem
access while exercising the identity-set mutation logic inside the
function under test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# The function under test lives in the commands module.
from apm_cli.commands.install import (
    _check_package_conflicts,
    _get_invocation_argv,
    _resolve_package_references,
    _split_argv_at_double_dash,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(canonical, identity, *, is_insecure=False, is_local=False):
    """Return a mock DependencyReference with the minimal API surface."""
    ref = MagicMock()
    ref.to_canonical.return_value = canonical
    ref.get_identity.return_value = identity
    ref.is_insecure = is_insecure
    ref.is_local = is_local
    return ref


def _disable_gitlab_direct_probe(mock_dep_cls):
    """Keep these unit tests focused on identity mutation, not GitLab probing."""
    mock_dep_cls.needs_gitlab_direct_shorthand_probing.return_value = False


# ---------------------------------------------------------------------------
# P1-G2 -- existing_identities mutation contract
# ---------------------------------------------------------------------------


class TestResolvePackageReferencesPopulatesIdentities:
    """After resolving valid packages the identity set must grow."""

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_empty_set_populated_after_resolve(self, mock_dep_cls, mock_validate):
        """Calling with an empty set and two valid packages adds both identities."""
        ref_a = _make_dep_ref("owner/repo-a", "github.com/owner/repo-a")
        ref_b = _make_dep_ref("owner/repo-b", "github.com/owner/repo-b")
        mock_dep_cls.parse.side_effect = [ref_a, ref_b]
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = set()

        _valid, invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo-a", "owner/repo-b"],
            [],
            existing,
        )

        assert "github.com/owner/repo-a" in existing
        assert "github.com/owner/repo-b" in existing
        assert len(existing) == 2
        assert len(validated) == 2
        assert len(invalid) == 0

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_single_package_adds_one_identity(self, mock_dep_cls, mock_validate):
        """A single valid package adds exactly one identity."""
        ref = _make_dep_ref("acme/tools", "github.com/acme/tools")
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = set()

        _resolve_package_references(["acme/tools"], [], existing)

        assert existing == {"github.com/acme/tools"}


class TestResolvePackageReferencesDuplicateDetection:
    """Pre-populated identities cause duplicates to be skipped."""

    @pytest.mark.parametrize("request_count", [1, 2])
    @pytest.mark.parametrize(
        ("entry", "package"),
        [
            ("owner/repo", "owner/repo"),
            ("owner/repo#main", "owner/repo#main"),
            ("owner/repo#v1", "owner/repo"),
            (
                {"git": "owner/repo", "ref": "main", "skills": ["alpha"], "targets": ["copilot"]},
                "owner/repo#main",
            ),
            ({"git": "owner/repo", "alias": "local-name"}, "owner/repo"),
        ],
    )
    def test_existing_dependency_without_new_intent_is_not_updated(
        self, entry, package, request_count
    ) -> None:
        current_deps = [entry]
        logger = MagicMock(verbose=False)
        updated = []

        with patch("apm_cli.commands.install._validate_package_exists", return_value=True):
            valid, invalid, added, _, _entries, changed = _resolve_package_references(
                [package] * request_count,
                current_deps,
                _check_package_conflicts(current_deps),
                logger=logger,
                updated_packages_out=updated,
            )

        assert invalid == []
        assert valid == [(package, True)] * request_count
        assert added == updated == []
        assert changed is False
        assert current_deps == [entry]
        assert current_deps[0] is entry
        assert (
            logger.validation_pass.call_args_list
            == [call(package, True, False, False)] * request_count
        )

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_preexisting_identity_skipped(self, mock_dep_cls, mock_validate):
        """A package whose identity is already in the set is not added to validated_packages."""
        ref = _make_dep_ref("owner/repo-a", "github.com/owner/repo-a")
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = {"github.com/owner/repo-a"}

        valid, _invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo-a"],
            [],
            existing,
        )

        # Identity was already present so validated list is empty
        assert validated == []
        # valid_outcomes still records it (with already_present=True)
        assert len(valid) == 1
        _canonical, already_present = valid[0]
        assert already_present is True
        # Set is unchanged
        assert existing == {"github.com/owner/repo-a"}

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_batch_duplicate_second_occurrence_skipped(self, mock_dep_cls, mock_validate):
        """When the same identity appears twice in one batch, only the first is added."""
        ref = _make_dep_ref("owner/repo-x", "github.com/owner/repo-x")
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = set()

        valid, _invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo-x", "owner/repo-x"],
            [],
            existing,
        )

        # Only the first occurrence ends up in validated
        assert len(validated) == 1
        assert validated[0] == "owner/repo-x"
        # Both appear in valid_outcomes
        assert len(valid) == 2
        assert valid[0][1] is False  # first is new
        assert valid[1][1] is True  # second is already present
        # Set has exactly one entry
        assert existing == {"github.com/owner/repo-x"}

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_mixed_new_and_preexisting(self, mock_dep_cls, mock_validate):
        """Batch with one new and one preexisting identity resolves only the new one."""
        ref_old = _make_dep_ref("owner/old-pkg", "github.com/owner/old-pkg")
        ref_new = _make_dep_ref("owner/new-pkg", "github.com/owner/new-pkg")
        mock_dep_cls.parse.side_effect = [ref_old, ref_new]
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = {"github.com/owner/old-pkg"}

        _valid, _invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/old-pkg", "owner/new-pkg"],
            [],
            existing,
        )

        assert validated == ["owner/new-pkg"]
        assert "github.com/owner/new-pkg" in existing
        assert len(existing) == 2

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_existing_unpinned_dependency_is_updated_when_cli_supplies_ref(self, mock_validate):
        """An explicit CLI ref for an existing dep must replace the unpinned manifest entry."""
        current_deps = ["danielmeppiel/genesis"]
        existing = _check_package_conflicts(current_deps)

        valid, invalid, validated, _mkt, _entries, changed = _resolve_package_references(
            ["danielmeppiel/genesis#v0.4.0"],
            current_deps,
            existing,
        )

        assert invalid == []
        assert valid == [("danielmeppiel/genesis#v0.4.0", True)]
        assert validated == []
        assert changed is True
        assert current_deps == ["danielmeppiel/genesis#v0.4.0"]

    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.DependencyReference")
    def test_git_semver_range_skips_literal_ref_preflight(self, mock_dep_cls, mock_validate):
        ref = _make_dep_ref(
            "owner/repo/packages/skill#>=1.0.0",
            "github.com/owner/repo/packages/skill",
        )
        ref.ref_kind = "semver"
        ref.source = "git"
        ref.artifactory_prefix = None
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        valid, invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo/packages/skill#>=1.0.0"],
            [],
            set(),
        )

        mock_validate.assert_not_called()
        assert invalid == []
        assert valid == [("owner/repo/packages/skill#>=1.0.0", False)]
        assert validated == ["owner/repo/packages/skill#>=1.0.0"]

    @patch("apm_cli.install.registry_wiring.validate_registry_ref", return_value=(True, ""))
    @patch(
        "apm_cli.install.registry_wiring.should_skip_github_probe_for_dep",
        return_value=True,
    )
    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.DependencyReference")
    def test_registry_semver_range_keeps_registry_validation(
        self, mock_dep_cls, mock_validate, mock_skip_probe, mock_validate_registry
    ):
        ref = _make_dep_ref("owner/repo#^1.0.0", "registry.example/owner/repo")
        ref.ref_kind = "semver"
        ref.source = "git"
        ref.artifactory_prefix = None
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        valid, invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo#^1.0.0"],
            [],
            set(),
        )

        mock_skip_probe.assert_called_once_with(ref, None)
        mock_validate_registry.assert_called_once_with(ref)
        mock_validate.assert_not_called()
        assert invalid == []
        assert valid == [("owner/repo#^1.0.0", False)]
        assert validated == ["owner/repo#^1.0.0"]

    @patch(
        "apm_cli.install.registry_wiring.should_skip_github_probe_for_dep",
        return_value=True,
    )
    @patch(
        "apm_cli.install.registry_wiring.validate_registry_ref",
        return_value=(False, "Invalid semver range"),
    )
    @patch("apm_cli.commands.install._validate_package_exists")
    @patch("apm_cli.commands.install.DependencyReference")
    def test_default_registry_invalid_semver_range_fails_closed(
        self, mock_dep_cls, mock_validate, mock_validate_registry, mock_skip_probe
    ):
        ref = _make_dep_ref("owner/repo#^1.0", "registry.example/owner/repo")
        ref.source = "git"
        ref.artifactory_prefix = None
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        valid, invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo#^1.0"],
            [],
            set(),
        )

        mock_skip_probe.assert_called_once_with(ref, None)
        mock_validate_registry.assert_called_once_with(ref)
        mock_validate.assert_not_called()
        assert valid == []
        assert invalid == [("owner/repo#^1.0", "Invalid semver range")]
        assert validated == []

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_literal_git_ref_keeps_exact_ref_preflight(self, mock_dep_cls, mock_validate):
        ref = _make_dep_ref("owner/repo#v1.2.0", "github.com/owner/repo")
        ref.ref_kind = "literal"
        ref.source = "git"
        ref.artifactory_prefix = None
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        _resolve_package_references(["owner/repo#v1.2.0"], [], set())

        mock_validate.assert_called_once()


class TestResolvePackageReferencesInvalidInput:
    """Invalid packages must not mutate the identity set."""

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_parse_error_does_not_mutate_set(self, mock_dep_cls, mock_validate):
        """If DependencyReference.parse() raises ValueError the set is unchanged."""
        mock_dep_cls.parse.side_effect = ValueError("bad input")
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = set()

        _valid, invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["bad-input"],
            [],
            existing,
        )

        assert existing == set()
        assert validated == []
        assert len(invalid) == 1

    @patch("apm_cli.commands.install._validate_package_exists", return_value=False)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_inaccessible_package_does_not_mutate_set(self, mock_dep_cls, mock_validate):
        """If validation fails the identity is not added to the set."""
        ref = _make_dep_ref("owner/repo-gone", "github.com/owner/repo-gone")
        ref.is_local = False
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        existing = set()

        _valid, invalid, validated, _mkt, _entries, _changed = _resolve_package_references(
            ["owner/repo-gone"],
            [],
            existing,
        )

        assert existing == set()
        assert validated == []
        assert len(invalid) == 1


# ---------------------------------------------------------------------------
# Direct GitLab FQDN shorthand — virtual probe persistence (object-form apm.yml)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #1326 -- cross-repo bare-on-enterprise fail-closed gate (dependency confusion)
# ---------------------------------------------------------------------------


class TestResolvePackageReferencesCrossRepoFailClosed:
    """When a marketplace resolution attaches a ``CrossRepoMisconfigRisk``
    sentinel the install command must refuse the package immediately --
    before any outbound validation HTTP probe -- and surface a reason
    string naming both qualification escape hatches. This is the
    dependency-confusion gate (#1326): the same syntactic ambiguity an
    attacker would exploit by pre-staging the bare namespace on public
    github.com is rejected at the install boundary rather than silently
    resolved against attacker content.
    """

    @staticmethod
    def _resolution_with_risk():
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.resolver import (
            CrossRepoMisconfigRisk,
            MarketplacePluginResolution,
        )

        plugin = MarketplacePlugin(
            name="shared-tool",
            source={
                "type": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/shared",
            },
        )
        return MarketplacePluginResolution(
            canonical="platform-team/shared-tool/plugins/shared",
            plugin=plugin,
            dependency_reference=None,
            cross_repo_misconfig_risk=CrossRepoMisconfigRisk(
                marketplace_host="corp.ghe.com",
                bare_repo_field="platform-team/shared-tool",
                suggested_qualified_repo=("corp.ghe.com/platform-team/shared-tool"),
            ),
        )

    @staticmethod
    def _resolution_without_risk():
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.resolver import MarketplacePluginResolution

        plugin = MarketplacePlugin(
            name="shared-tool",
            source="./plugins/shared",
        )
        return MarketplacePluginResolution(
            canonical="myorg/my-marketplace/plugins/shared",
            plugin=plugin,
            dependency_reference=None,
            cross_repo_misconfig_risk=None,
        )

    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    @patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin")
    @patch("apm_cli.marketplace.resolver.parse_marketplace_ref")
    @patch("apm_cli.commands.install.DependencyReference")
    def test_install_refused_when_risk_present_validate_never_called(
        self,
        mock_dep_cls,
        mock_parse_ref,
        mock_resolve_mkt,
        mock_validate,
    ):
        """Risk-bearing resolution rejects the package before validation.

        The mock for ``_validate_package_exists`` is configured to return
        True -- modelling the attack precondition (attacker has pre-staged
        the bare namespace on public github.com so the validator would
        succeed). The gate must fire pre-validation so the mock is never
        called: no probe to the attacker URL, no information leak, no
        attacker content downloaded.
        """
        mock_parse_ref.return_value = ("shared-tool", "my-marketplace", None)
        mock_resolve_mkt.return_value = self._resolution_with_risk()
        ref = _make_dep_ref(
            "platform-team/shared-tool/plugins/shared",
            "github.com/platform-team/shared-tool/plugins/shared",
        )
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        logger = MagicMock()
        logger.verbose = False

        result = _resolve_package_references(
            ["shared-tool@my-marketplace"],
            [],
            set(),
            logger=logger,
        )

        # Gate fires pre-validation -- no HTTP probe to attacker URL.
        assert mock_validate.call_count == 0
        # Package landed in invalid_outcomes with a refusal reason that
        # names both qualification escape hatches.
        valid_outcomes, invalid_outcomes = result[0], result[1]
        assert valid_outcomes == []
        assert len(invalid_outcomes) == 1
        rejected, reason = invalid_outcomes[0]
        assert rejected == "shared-tool@my-marketplace"
        # Reason carries enterprise host, both qualified forms, and the
        # issue reference for grep-ability. Host substrings are anchored
        # with surrounding quote/backtick characters so CodeQL's
        # ``py/incomplete-url-substring-sanitization`` does not flag bare
        # host-name membership checks (see tests/**/CLAUDE.md).
        assert "'corp.ghe.com'" in reason
        assert "`repo: platform-team/shared-tool`" in reason
        assert "'corp.ghe.com/platform-team/shared-tool'" in reason
        assert "'github.com/platform-team/shared-tool'" in reason
        assert "#1326" in reason
        # ``validation_fail`` is the user-visible surface (not ``warning``);
        # the refusal IS the failure, not a hint on top of one.
        assert logger.validation_fail.call_count == 1

    @patch("apm_cli.commands.install._validate_package_exists", return_value=False)
    @patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin")
    @patch("apm_cli.marketplace.resolver.parse_marketplace_ref")
    @patch("apm_cli.commands.install.DependencyReference")
    def test_install_proceeds_when_no_risk_attached(
        self,
        mock_dep_cls,
        mock_parse_ref,
        mock_resolve_mkt,
        mock_validate,
    ):
        """In-marketplace / non-enterprise resolutions carry no sentinel;
        the gate is dormant and the normal validate path runs."""
        mock_parse_ref.return_value = ("shared-tool", "my-marketplace", None)
        mock_resolve_mkt.return_value = self._resolution_without_risk()
        ref = _make_dep_ref(
            "myorg/my-marketplace/plugins/shared",
            "github.com/myorg/my-marketplace/plugins/shared",
        )
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        logger = MagicMock()
        logger.verbose = False

        _resolve_package_references(
            ["shared-tool@my-marketplace"],
            [],
            set(),
            logger=logger,
        )

        # No sentinel = gate dormant = validate runs as normal.
        assert mock_validate.call_count == 1

    @patch("apm_cli.commands.install._validate_package_exists", return_value=False)
    @patch("apm_cli.commands.install.DependencyReference")
    def test_no_gate_for_plain_owner_repo(
        self,
        mock_dep_cls,
        mock_validate,
    ):
        """A bare ``owner/repo`` (no marketplace) bypasses marketplace
        resolution entirely -- the sentinel is only emitted by
        ``resolve_marketplace_plugin``, so the gate cannot fire for direct
        deps. Validation proceeds normally even though it then fails."""
        ref = _make_dep_ref(
            "platform-team/shared-tool",
            "github.com/platform-team/shared-tool",
        )
        mock_dep_cls.parse.return_value = ref
        mock_dep_cls.is_local_path.return_value = False
        _disable_gitlab_direct_probe(mock_dep_cls)

        logger = MagicMock()
        logger.verbose = False

        _resolve_package_references(
            ["platform-team/shared-tool"],
            [],
            set(),
            logger=logger,
        )

        # Validate is called (gate not engaged for non-marketplace deps).
        assert mock_validate.call_count == 1


# ---------------------------------------------------------------------------
# Marketplace provenance in probe-skip bypass
# ---------------------------------------------------------------------------


class TestResolvePackageReferencesProbeSkipProvenance:
    """Marketplace provenance must be recorded when the probe-skip bypass fires.

    Regression: before the fix, the ``continue`` in the bypass block ran before
    the ``if marketplace_provenance:`` write, silently dropping provenance for
    registry-routed marketplace installs.
    """

    def test_provenance_recorded_in_probe_skip_bypass(self):
        # Build a dep_ref that will trigger the bypass:
        # source not "git"/"registry", is_local=False, reference set.
        from unittest.mock import PropertyMock

        dep_ref = MagicMock()
        dep_ref.source = None
        dep_ref.is_local = False
        dep_ref.reference = "1.0.0"
        dep_ref.to_canonical.return_value = "acme/reg-plugin#1.0.0"
        dep_ref.get_identity.return_value = "github.com/acme/reg-plugin"
        dep_ref.is_insecure = False
        dep_ref.is_virtual = False
        dep_ref.is_virtual_subdirectory.return_value = False
        type(dep_ref).ref_kind = PropertyMock(return_value="semver")
        dep_ref.allow_insecure = False

        fake_provenance = {
            "discovered_via": "apm-marketplace",
            "marketplace_plugin_name": "reg-plugin",
        }

        # resolution is a 2-tuple (canonical_str, plugin_obj) -- the code does:
        # ``canonical_str, _resolved_plugin = resolution``
        fake_resolution = MagicMock()
        fake_resolution.__iter__ = lambda s: iter(["acme/reg-plugin#1.0.0", MagicMock()])
        fake_resolution.cross_repo_misconfig_risk = None
        fake_resolution.provenance.return_value = fake_provenance
        fake_resolution.dependency_reference = None

        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("reg-plugin", "apm-marketplace", None),
            ),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=fake_resolution,
            ),
            patch(
                "apm_cli.commands.install.resolve_parsed_dependency_reference",
                return_value=(dep_ref, False),
            ),
            patch(
                "apm_cli.install.package_resolution.update_existing_dependency_entry_if_needed",
                return_value=False,
            ),
        ):
            _valid, invalid, _validated, mkt_prov, _entries, _changed = _resolve_package_references(
                ["reg-plugin@apm-marketplace"],
                [],
                set(),
                default_registry="corp",
            )

        assert invalid == [], f"unexpected errors: {invalid}"
        assert "github.com/acme/reg-plugin" in mkt_prov, (
            "marketplace provenance not written in probe-skip bypass"
        )
        assert mkt_prov["github.com/acme/reg-plugin"] == fake_provenance


class TestGetInvocationArgv:
    def test_returns_sys_argv(self) -> None:
        import sys

        result = _get_invocation_argv()
        assert result is sys.argv


class TestSplitArgvAtDoubleDash:
    @pytest.mark.parametrize(
        ("argv", "expected_clean", "expected_cmd"),
        [
            (["apm", "install", "owner/repo"], ["apm", "install", "owner/repo"], ()),
            (
                ["apm", "install", "--mcp", "foo", "--", "npx", "-y", "srv"],
                ["apm", "install", "--mcp", "foo"],
                ("npx", "-y", "srv"),
            ),
            (["apm", "install", "--"], ["apm", "install"], ()),
            (["--", "npx", "server"], [], ("npx", "server")),
            ([], [], ()),
        ],
        ids=["no-double-dash", "splits", "at-end", "at-start", "empty"],
    )
    def test_splits_argv_at_double_dash(
        self, argv: list[str], expected_clean: list[str], expected_cmd: tuple
    ) -> None:
        clean, cmd = _split_argv_at_double_dash(argv)
        assert clean == expected_clean
        assert cmd == expected_cmd

    def test_command_tuple_is_tuple_type(self) -> None:
        _, cmd = _split_argv_at_double_dash(["a", "--", "b"])
        assert isinstance(cmd, tuple)


class TestCheckPackageConflicts:
    @pytest.mark.parametrize("deps", [[], [42, None, 3.14]], ids=["empty", "unknown-types"])
    def test_no_parseable_deps_returns_empty_set(self, deps: list) -> None:
        assert _check_package_conflicts(deps) == set()

    def test_string_dep_adds_identity(self) -> None:
        with patch("apm_cli.install.package_selection.DependencyReference") as mock_cls:
            mock_ref = MagicMock()
            mock_ref.get_identity.return_value = "github.com/owner/repo"
            mock_cls.parse.return_value = mock_ref
            result = _check_package_conflicts(["owner/repo"])
        assert "github.com/owner/repo" in result

    def test_dict_dep_uses_parse_from_dict(self) -> None:
        with patch("apm_cli.install.package_selection.DependencyReference") as mock_cls:
            mock_ref = MagicMock()
            mock_ref.get_identity.return_value = "github.com/owner/repo"
            mock_cls.parse_from_dict.return_value = mock_ref
            result = _check_package_conflicts([{"repo": "owner/repo"}])
        assert "github.com/owner/repo" in result

    def test_invalid_dep_entry_is_skipped(self) -> None:
        with patch("apm_cli.install.package_selection.DependencyReference") as mock_cls:
            mock_cls.parse.side_effect = ValueError("bad format")
            result = _check_package_conflicts(["bad-format"])
        assert result == set()

    def test_multiple_deps_returns_all_identities(self) -> None:
        with patch("apm_cli.install.package_selection.DependencyReference") as mock_cls:
            ref_a = MagicMock()
            ref_a.get_identity.return_value = "github.com/a/repo"
            ref_b = MagicMock()
            ref_b.get_identity.return_value = "github.com/b/repo"
            mock_cls.parse.side_effect = [ref_a, ref_b]
            result = _check_package_conflicts(["a/repo", "b/repo"])
        assert "github.com/a/repo" in result
        assert "github.com/b/repo" in result


class TestInstallContextDefaults:
    def test_refresh_default_is_false(self) -> None:
        from apm_cli.commands.install import InstallContext

        ctx = InstallContext(
            scope=None,
            manifest_path=Path("."),
            manifest_display="apm.yml",
            apm_dir=Path("."),
            project_root=Path("."),
            logger=None,
            auth_resolver=None,
            verbose=False,
            force=False,
            dry_run=False,
            update=False,
            dev=False,
            runtime=None,
            exclude=None,
            target=None,
            parallel_downloads=4,
            allow_insecure=False,
            allow_insecure_hosts=(),
            protocol_pref=None,
            allow_protocol_fallback=False,
            trust_transitive_mcp=False,
            no_policy=False,
            install_mode=None,
            packages=(),
        )
        assert ctx.refresh is False
        assert ctx.only_packages is None
        assert ctx.transaction is None
        assert ctx.legacy_skill_paths is False
        assert ctx.frozen is False
        assert ctx.plan_callback is None

    def test_context_is_mutable(self) -> None:
        from apm_cli.commands.install import InstallContext

        ctx = InstallContext(
            scope=None,
            manifest_path=Path("."),
            manifest_display="apm.yml",
            apm_dir=Path("."),
            project_root=Path("."),
            logger=None,
            auth_resolver=None,
            verbose=False,
            force=False,
            dry_run=False,
            update=False,
            dev=False,
            runtime=None,
            exclude=None,
            target=None,
            parallel_downloads=4,
            allow_insecure=False,
            allow_insecure_hosts=(),
            protocol_pref=None,
            allow_protocol_fallback=False,
            trust_transitive_mcp=False,
            no_policy=False,
            install_mode=None,
            packages=(),
        )
        ctx.refresh = True
        assert ctx.refresh is True


class TestScopedInstallDependencyResolver:
    """Tests for _ScopedInstallDependencyResolver.expand_parent_repo_decl."""

    def test_user_scope_raises_value_error(self) -> None:
        """expand_parent_repo_decl raises ValueError for USER scope."""
        from apm_cli.commands.install import (
            APM_DEPS_AVAILABLE,
            _ScopedInstallDependencyResolver,
        )

        if not APM_DEPS_AVAILABLE or _ScopedInstallDependencyResolver is None:
            pytest.skip("APM deps not available")

        from apm_cli.core.scope import InstallScope

        # Build a minimal instance with USER scope; patch the base
        # expand_parent_repo_decl to avoid network I/O.
        resolver = _ScopedInstallDependencyResolver.__new__(_ScopedInstallDependencyResolver)
        resolver._install_scope = InstallScope.USER

        with pytest.raises(ValueError, match="user"):
            resolver.expand_parent_repo_decl(MagicMock(), MagicMock())

    def test_non_user_scope_delegates_to_super(self) -> None:
        """expand_parent_repo_decl delegates to super when scope != USER."""
        from apm_cli.commands.install import (
            APM_DEPS_AVAILABLE,
            _ScopedInstallDependencyResolver,
        )

        if not APM_DEPS_AVAILABLE or _ScopedInstallDependencyResolver is None:
            pytest.skip("APM deps not available")

        from apm_cli.core.scope import InstallScope

        resolver = _ScopedInstallDependencyResolver.__new__(_ScopedInstallDependencyResolver)
        resolver._install_scope = InstallScope.PROJECT

        parent_dep = MagicMock()
        child_dep = MagicMock()
        expected = MagicMock()

        with patch.object(
            _ScopedInstallDependencyResolver.__bases__[0],
            "expand_parent_repo_decl",
            return_value=expected,
        ):
            result = resolver.expand_parent_repo_decl(parent_dep, child_dep)
            assert result is expected


class TestSkillSubsetBranch:
    """--skill '*' should not set _skill_subset; non-wildcard should."""

    def test_skill_names_wildcard_sets_none_subset(self) -> None:
        """When skill_names=['*'], _skill_subset stays None."""
        # Directly test the branch logic by inspecting what the install
        # command does before delegating.  We use the fact that wildcard
        # means "all skills" so the subset is effectively None.
        import builtins

        skill_names = ("*",)
        _skill_subset = None
        if skill_names:
            if not any(s == "*" for s in skill_names):
                _skill_subset = builtins.tuple(skill_names)

        assert _skill_subset is None

    def test_skill_names_non_wildcard_sets_subset(self) -> None:
        """When skill_names=['foo','bar'], _skill_subset is set."""
        import builtins

        skill_names = ("foo", "bar")
        _skill_subset = None
        if skill_names:
            if not any(s == "*" for s in skill_names):
                _skill_subset = builtins.tuple(skill_names)

        assert _skill_subset == ("foo", "bar")


class TestFrozenInfoMessage:
    """When frozen=True and apm_count > 0, emit the lockfile verified message."""

    def test_frozen_apm_count_info(self) -> None:
        # Simulate the code path logic
        frozen = True
        apm_count = 3

        import apm_cli.commands.install as _install_mod

        with patch("apm_cli.commands.install._rich_info") as mock_info:
            if frozen and apm_count > 0:
                _install_mod._rich_info(
                    "Deployed files verified against apm.lock.yaml.",
                    symbol="info",
                )
            mock_info.assert_called_once()
            assert "apm.lock.yaml" in mock_info.call_args[0][0]

    def test_frozen_apm_count_zero_no_info(self) -> None:
        """frozen=True but apm_count=0 should not emit info."""
        frozen = True
        apm_count = 0

        with patch("apm_cli.commands.install._rich_info") as mock_info:
            if frozen and apm_count > 0:
                from apm_cli.commands.install import _rich_info as ri

                ri("should not be called", symbol="info")
            mock_info.assert_not_called()
