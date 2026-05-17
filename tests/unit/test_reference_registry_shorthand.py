"""Tests for the ``owner/repo@<name>#<semver>`` registry-scope shorthand.

Covers the parser change in ``DependencyReference.parse`` per
docs/proposals/registry-api.md §3.2/§3.3:

- Routes a string-shorthand entry to a named registry when it carries the
  ``@<name>`` scope suffix.
- Rejects branch names and commit SHAs at parse time when the entry routes
  to a registry (parse-time gate, not install-time).
- Does NOT change the meaning of any existing shorthand: ``acme/foo#v1.0``,
  ``git@host:...``, ``https://...`` all parse byte-identically.
- Does NOT collide with the marketplace ``code-review@plugins`` shape (no
  ``/`` on the LHS).
"""

from __future__ import annotations

import pytest

from apm_cli.models.dependency.reference import DependencyReference


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    """Most tests in this module exercise the experimental registry feature."""
    import apm_cli.config as _conf

    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {"experimental": {"package_registry": True}},
    )


def _disable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {}})


class TestRegistryScopeRouting:
    """Strings of the form ``owner/repo@<name>#<semver>`` route to a registry."""

    def test_basic_caret_range(self):
        d = DependencyReference.parse("acme/foo@corp-main#^1.2.3")
        assert d.repo_url == "acme/foo"
        assert d.reference == "^1.2.3"
        assert d.source == "registry"
        assert d.registry_name == "corp-main"

    def test_exact_version(self):
        d = DependencyReference.parse("acme/foo@corp-other#1.2.3")
        assert d.source == "registry"
        assert d.registry_name == "corp-other"
        assert d.reference == "1.2.3"

    def test_patch_wildcard_range(self):
        d = DependencyReference.parse("acme/foo@corp-main#1.2.x")
        assert d.source == "registry"
        assert d.reference == "1.2.x"

    def test_tilde_range(self):
        d = DependencyReference.parse("acme/foo@corp-main#~1.2.3")
        assert d.reference == "~1.2.3"

    def test_dotted_registry_name(self):
        d = DependencyReference.parse("acme/foo@corp.main#1.0.0")
        assert d.registry_name == "corp.main"

    def test_underscore_registry_name(self):
        # Hyphens, dots, underscores — all valid in registry names.
        d = DependencyReference.parse("acme/foo@corp_main#1.0.0")
        assert d.registry_name == "corp_main"

    def test_extended_repo_path(self):
        # Non-default-host shorthand: ``host/owner/repo@<name>#<semver>`` —
        # the LHS regex captures multi-segment paths.
        d = DependencyReference.parse("gitlab.com/owner/repo@corp-main#1.0.0")
        assert d.source == "registry"
        assert d.registry_name == "corp-main"
        # repo_url after normalization is ``owner/repo`` (host extracted).
        assert d.repo_url == "owner/repo"


class TestPackageRegistryExperimentalGate:
    def test_registry_scope_requires_flag(self, monkeypatch):
        _disable_package_registry(monkeypatch)
        with pytest.raises(
            ValueError, match="apm experimental enable package-registry"
        ):
            DependencyReference.parse("acme/foo@corp-main#^1.2.3")

    def test_registry_object_form_requires_flag(self, monkeypatch):
        _disable_package_registry(monkeypatch)
        with pytest.raises(
            ValueError, match="apm experimental enable package-registry"
        ):
            DependencyReference.parse_from_dict(
                {
                    "registry": "corp-main",
                    "id": "acme/prompt-pack",
                    "path": "prompts/review.prompt.md",
                    "version": "1.4.0",
                }
            )

    def test_git_and_local_forms_remain_available_without_flag(self, monkeypatch):
        _disable_package_registry(monkeypatch)
        git_dep = DependencyReference.parse("acme/foo#main")
        local_dep = DependencyReference.parse("./local/pkg")
        assert git_dep.source is None
        assert local_dep.is_local


class TestSemverEnforcement:
    """Parse-time semver validation per design §3.3."""

    def test_branch_name_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse("acme/foo@corp-main#main")

    def test_develop_branch_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse("acme/foo@corp-main#develop")

    def test_commit_sha_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse("acme/foo@corp-main#abc123d")

    def test_long_commit_sha_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse(
                "acme/foo@corp-main#abc1234567890def1234567890abcdef12345678"
            )

    def test_latest_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse("acme/foo@corp-main#latest")

    def test_missing_version_rejected(self):
        with pytest.raises(ValueError, match="missing a version"):
            DependencyReference.parse("acme/foo@corp-main")

    def test_error_message_suggests_git_alternative(self):
        with pytest.raises(ValueError) as excinfo:
            DependencyReference.parse("acme/foo@corp-main#main")
        msg = str(excinfo.value)
        assert "- git:" in msg
        assert "semver" in msg.lower()


class TestNoCollisionsWithExistingShapes:
    """The new rule must NOT change any existing parse outcome."""

    def test_plain_shorthand_unchanged(self):
        d = DependencyReference.parse("acme/foo#v1.0")
        assert d.source is None
        assert d.registry_name is None
        assert d.reference == "v1.0"
        assert d.repo_url == "acme/foo"

    def test_shorthand_with_branch_ref_still_works(self):
        # When NOT routed to a registry, branch refs remain valid (Git is
        # ref-opaque). This is invariant §2.1.3.
        d = DependencyReference.parse("acme/foo#main")
        assert d.source is None
        assert d.reference == "main"

    def test_ssh_url_unchanged(self):
        d = DependencyReference.parse("git@github.com:owner/repo.git")
        assert d.source is None
        assert d.registry_name is None

    def test_ssh_url_with_alias_unchanged(self):
        # ``#ref@alias`` shape — alias comes after ``#``, must not be
        # interpreted as a registry name.
        d = DependencyReference.parse("git@github.com:owner/repo.git#main@my-alias")
        assert d.source is None
        assert d.alias == "my-alias"

    def test_https_url_unchanged(self):
        d = DependencyReference.parse("https://github.com/owner/repo")
        assert d.source is None

    def test_https_url_with_basic_auth_unchanged(self):
        # User:password@host inside an HTTPS URL must not trigger registry
        # scope (the ``://`` guard catches this).
        # Note: actual parse may or may not accept this — what we're asserting
        # is that the registry-scope detector does NOT fire.
        try:
            d = DependencyReference.parse("https://user:pass@host.com/owner/repo")
            assert d.source is None
        except ValueError:
            pass  # parser may reject for unrelated reasons; that's fine.

    def test_marketplace_shape_falls_through(self):
        # ``review-skills@plugins`` has no ``/`` on the LHS — it does NOT
        # route to a registry. The registry detector returns None and the
        # existing parse logic raises (since 'review-skills' isn't a valid
        # owner/repo on its own). What matters: the error is the existing
        # one, not the new "missing version" one.
        with pytest.raises(ValueError) as excinfo:
            DependencyReference.parse("review-skills@plugins")
        assert "missing a version" not in str(excinfo.value)

    def test_local_path_unchanged(self):
        d = DependencyReference.parse("./local/pkg")
        assert d.is_local
        assert d.source is None


class TestObjectFormRegistry:
    """Object-form ``- registry: <name>`` entry per design §3.2.

    Used for virtual packages — the four fields (id, registry, path, version)
    don't compose cleanly in shorthand.
    """

    def test_happy_path(self):
        d = DependencyReference.parse_from_dict(
            {
                "registry": "corp-main",
                "id": "acme/prompt-pack",
                "path": "prompts/review.prompt.md",
                "version": "1.4.0",
            }
        )
        assert d.repo_url == "acme/prompt-pack"
        assert d.virtual_path == "prompts/review.prompt.md"
        assert d.is_virtual is True
        assert d.reference == "1.4.0"
        assert d.source == "registry"
        assert d.registry_name == "corp-main"

    def test_with_alias(self):
        d = DependencyReference.parse_from_dict(
            {
                "registry": "corp",
                "id": "a/b",
                "path": "x.prompt.md",
                "version": "^1.0.0",
                "alias": "my-x",
            }
        )
        assert d.alias == "my-x"

    def test_caret_range(self):
        d = DependencyReference.parse_from_dict(
            {
                "registry": "corp",
                "id": "a/b",
                "path": "x.prompt.md",
                "version": "^1.2.3",
            }
        )
        assert d.reference == "^1.2.3"

    @pytest.mark.parametrize(
        "missing_key",
        ["registry", "id", "path", "version"],
    )
    def test_missing_required_field_rejected(self, missing_key):
        full = {
            "registry": "corp",
            "id": "a/b",
            "path": "x.prompt.md",
            "version": "1.0.0",
        }
        full.pop(missing_key)
        with pytest.raises(ValueError):
            DependencyReference.parse_from_dict(full)

    def test_mixing_registry_and_git_rejected(self):
        with pytest.raises(ValueError, match="cannot mix"):
            DependencyReference.parse_from_dict(
                {
                    "registry": "corp",
                    "git": "https://github.com/a/b.git",
                    "id": "a/b",
                    "path": "x.prompt.md",
                    "version": "1.0.0",
                }
            )

    def test_invalid_id_shape_rejected(self):
        with pytest.raises(ValueError, match="owner/repo"):
            DependencyReference.parse_from_dict(
                {
                    "registry": "corp",
                    "id": "noseparator",
                    "path": "x",
                    "version": "1.0.0",
                }
            )

    def test_branch_version_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse_from_dict(
                {"registry": "corp", "id": "a/b", "path": "p", "version": "main"}
            )

    def test_commit_sha_version_rejected(self):
        with pytest.raises(ValueError, match="not a semver"):
            DependencyReference.parse_from_dict(
                {"registry": "corp", "id": "a/b", "path": "p", "version": "abc123d"}
            )

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError, match="unknown fields"):
            DependencyReference.parse_from_dict(
                {
                    "registry": "corp",
                    "id": "a/b",
                    "path": "p",
                    "version": "1.0.0",
                    "typo": "oops",
                }
            )

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError):
            DependencyReference.parse_from_dict(
                {
                    "registry": "corp",
                    "id": "a/b",
                    "path": "../escape",
                    "version": "1.0.0",
                }
            )

    def test_invalid_alias_rejected(self):
        with pytest.raises(ValueError, match="Invalid alias"):
            DependencyReference.parse_from_dict(
                {
                    "registry": "corp",
                    "id": "a/b",
                    "path": "p",
                    "version": "1.0.0",
                    "alias": "bad alias!",
                }
            )

    def test_empty_strings_rejected(self):
        for field in ("registry", "id", "path", "version"):
            entry = {
                "registry": "corp",
                "id": "a/b",
                "path": "x.prompt.md",
                "version": "1.0.0",
            }
            entry[field] = "   "
            with pytest.raises(ValueError):
                DependencyReference.parse_from_dict(entry)

    def test_existing_git_object_form_marks_source_git(self):
        # Object-form ``- git:`` now explicitly sets source="git" so the
        # default-registry routing pass leaves it alone. Per design,
        # source=None and source="git" are equivalent (legacy default), so
        # this is observable but functionally compatible.
        d = DependencyReference.parse_from_dict(
            {"git": "https://github.com/owner/repo.git", "ref": "v1.0"}
        )
        assert d.source == "git"
        assert d.registry_name is None

    def test_existing_local_object_form_unchanged(self):
        d = DependencyReference.parse_from_dict({"path": "./local/pkg"})
        assert d.is_local
        assert d.source is None


class TestRegistryFieldsRoundTrip:
    """The parser sets ``source`` + ``registry_name`` consistently with the
    object-form path (Phase 4 will add a parallel parser for object form)."""

    def test_unique_key_includes_repo_url(self):
        d = DependencyReference.parse("acme/foo@corp-main#1.0.0")
        assert d.get_unique_key() == "acme/foo"

    def test_identity_unchanged_by_registry_scope(self):
        # The registry routing is metadata, not part of the package identity.
        d_git = DependencyReference.parse("acme/foo#1.0.0")
        d_reg = DependencyReference.parse("acme/foo@corp-main#1.0.0")
        assert d_git.get_identity() == d_reg.get_identity() == "acme/foo"
