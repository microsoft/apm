"""Tests for registry-only ref enumeration (PROXY_REGISTRY_ONLY guard).

Issue #3448: with ``PROXY_REGISTRY_ONLY=1`` the CLI still enumerated refs
from the upstream host. PR #3455 fixed the ``apm view ... versions``
command surface; these tests cover the two remaining enumeration paths:

1. ``GitReferenceResolver._list_remote_refs`` -- used by ``apm outdated``
   and the downloader. Returns an empty list instead of running
   ``git ls-remote``, matching the existing ``is_artifactory()``
   short-circuit.
2. ``GitSemverResolver.resolve`` -- used by ``apm install`` / ``apm lock``
   for semver ranges. Raises ``RegistryOnlySemverError`` instead of
   listing upstream tags, because a range needs a version listing the
   proxy does not expose.

The assertion in both cases is that *no upstream call is attempted*, not
merely that the result changed.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.git_reference_resolver import GitReferenceResolver
from apm_cli.deps.git_semver_resolver import (
    GitSemverResolver,
    NoMatchingTagError,
    RegistryOnlySemverError,
)


def _dep_ref(*, artifactory: bool = False) -> MagicMock:
    """Build a GitHub-shorthand dependency reference.

    Deliberately *not* Artifactory-prefixed: that is the case the
    existing short-circuit already covers, and the regression in #3448
    only affects references that fall through it.
    """
    dep_ref = MagicMock()
    dep_ref.is_artifactory.return_value = artifactory
    dep_ref.is_azure_devops.return_value = False
    dep_ref.repo_url = "myorg/my-package"
    dep_ref.host = "github.com"
    dep_ref.reference = "1.4.x"
    return dep_ref


class TestGitReferenceResolverRegistryOnly:
    """``GitReferenceResolver`` must not ls-remote in registry-only mode."""

    def test_returns_empty_and_runs_no_git_command(self):
        host = MagicMock()
        resolver = GitReferenceResolver(host)

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch("apm_cli.deps.github_downloader.git") as git_module:
                refs = resolver.list_remote_refs(_dep_ref())

        assert refs == []
        git_module.cmd.Git.assert_not_called()

    def test_tags_only_listing_also_guarded(self):
        host = MagicMock()
        resolver = GitReferenceResolver(host)

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch("apm_cli.deps.github_downloader.git") as git_module:
                refs = resolver.list_remote_tag_refs(_dep_ref())

        assert refs == []
        git_module.cmd.Git.assert_not_called()

    def test_deprecated_artifactory_only_flag_honoured(self):
        """``is_enforce_only()`` accepts the deprecated alias."""
        host = MagicMock()
        resolver = GitReferenceResolver(host)

        with patch.dict(os.environ, {"ARTIFACTORY_ONLY": "true"}, clear=False):
            os.environ.pop("PROXY_REGISTRY_ONLY", None)
            with patch("apm_cli.deps.github_downloader.git") as git_module:
                refs = resolver.list_remote_refs(_dep_ref())

        assert refs == []
        git_module.cmd.Git.assert_not_called()

    def test_without_the_flag_enumeration_is_attempted(self):
        """Guard must not fire by default -- regression trap for the fix."""
        host = MagicMock()
        resolver = GitReferenceResolver(host)

        env = {k: v for k, v in os.environ.items()}
        env.pop("PROXY_REGISTRY_ONLY", None)
        env.pop("ARTIFACTORY_ONLY", None)

        with patch.dict(os.environ, env, clear=True):
            with patch("apm_cli.deps.github_downloader.git") as git_module:
                git_module.cmd.Git.return_value.ls_remote.return_value = ""
                resolver.list_remote_refs(_dep_ref())

        git_module.cmd.Git.assert_called()


class TestGitSemverResolverRegistryOnly:
    """``GitSemverResolver`` must refuse ranges rather than query upstream."""

    def test_raises_and_never_lists_refs(self):
        ref_resolver = MagicMock()
        resolver = GitSemverResolver(ref_resolver)

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with pytest.raises(RegistryOnlySemverError) as excinfo:
                resolver.resolve(
                    owner_repo="myorg/my-package",
                    package_name="my-package",
                    constraint="^1.0.0",
                )

        ref_resolver.list_remote_refs.assert_not_called()
        assert "registry-only mode" in excinfo.value.summary
        assert "PROXY_REGISTRY_ONLY" in excinfo.value.hint
        # The hint must name both escape hatches.
        assert "Pin an exact version" in excinfo.value.hint

    def test_error_is_catchable_as_no_matching_tag(self):
        """Existing callers catching NoMatchingTagError keep working."""
        ref_resolver = MagicMock()
        resolver = GitSemverResolver(ref_resolver)

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with pytest.raises(NoMatchingTagError):
                resolver.resolve(
                    owner_repo="myorg/my-package",
                    package_name="my-package",
                    constraint="1.4.x",
                )

        ref_resolver.list_remote_refs.assert_not_called()

    def test_without_the_flag_refs_are_listed(self):
        """Guard must not fire by default -- regression trap for the fix."""
        ref_resolver = MagicMock()
        ref_resolver.list_remote_refs.return_value = []
        resolver = GitSemverResolver(ref_resolver)

        env = {k: v for k, v in os.environ.items()}
        env.pop("PROXY_REGISTRY_ONLY", None)
        env.pop("ARTIFACTORY_ONLY", None)

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(NoMatchingTagError) as excinfo:
                resolver.resolve(
                    owner_repo="myorg/my-package",
                    package_name="my-package",
                    constraint="^1.0.0",
                )

        ref_resolver.list_remote_refs.assert_called_once()
        assert not isinstance(excinfo.value, RegistryOnlySemverError)
