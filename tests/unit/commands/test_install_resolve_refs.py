"""Unit tests for _resolve_package_references() mutation contract.

Covers P1-G2: the function mutates *existing_identities* in-place to
detect batch duplicates, and that contract was previously untested.

Strategy: mock ``DependencyReference.parse()`` and
``_validate_package_exists()`` so tests run without network or filesystem
access while exercising the identity-set mutation logic inside the
function under test.
"""

from __future__ import annotations

from textwrap import dedent
from unittest.mock import MagicMock, patch

# The function under test lives in the commands module.
from apm_cli.commands.install import _resolve_package_references
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.models.dependency.reference import DependencyReference

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


class TestResolvePackageReferencesGitLabDirectShorthandPersistence:
    """Virtual probe results must serialize as git+path for apm.yml merge."""

    _PKG = "git.epam.com/epm-ease/apm-registry/agents/ai-run-ba-flow"

    @patch("apm_cli.commands.install._try_resolve_gitlab_direct_shorthand")
    @patch("apm_cli.commands.install._validate_package_exists", return_value=True)
    def test_virtual_shorthand_populates_apm_yml_entries_object_form(
        self, mock_validate, mock_try_resolve
    ):
        resolved = DependencyReference.from_gitlab_shorthand_probe(
            "git.epam.com",
            "epm-ease/apm-registry",
            "agents/ai-run-ba-flow",
            None,
        )
        mock_try_resolve.return_value = resolved
        parsed_stub = MagicMock()
        parsed_stub.is_virtual = False

        with patch("apm_cli.commands.install.DependencyReference") as mock_dep_cls:
            mock_dep_cls.parse.return_value = parsed_stub
            mock_dep_cls.is_local_path.return_value = False
            mock_dep_cls.needs_gitlab_direct_shorthand_probing.return_value = True

            existing = set()
            _v, _inv, validated, _mkt, entries, _chg = _resolve_package_references(
                [self._PKG],
                [],
                existing,
            )

        canonical = "git.epam.com/epm-ease/apm-registry/agents/ai-run-ba-flow"
        assert validated == [canonical]
        assert entries[canonical] == {
            "git": "https://git.epam.com/epm-ease/apm-registry",
            "path": "agents/ai-run-ba-flow",
        }

    def test_generated_entry_round_trips_via_from_apm_yml(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            dedent(
                """
                name: wire-test
                version: "0.0.1"
                dependencies:
                  apm:
                    - git: https://git.epam.com/epm-ease/apm-registry
                      path: agents/ai-run-ba-flow
                """
            ).lstrip(),
            encoding="utf-8",
        )
        clear_apm_yml_cache()
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        ref = deps[0]
        assert ref.host == "git.epam.com"
        assert ref.repo_url == "epm-ease/apm-registry"
        assert ref.virtual_path == "agents/ai-run-ba-flow"
        assert ref.is_virtual is True
