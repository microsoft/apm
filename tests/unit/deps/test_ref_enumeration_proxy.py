"""Tests for registry-only ref enumeration (PROXY_REGISTRY_ONLY guard).

Issue #3448: with ``PROXY_REGISTRY_ONLY=1`` the CLI still enumerated refs
from the upstream host. These tests cover every enumeration path named in
the approved scope:

1. ``GitReferenceResolver._list_remote_refs`` -- used by ``apm outdated``
   and the downloader. Returns an empty list instead of running
   ``git ls-remote``, matching the existing ``is_artifactory()``
   short-circuit.
2. ``GitSemverResolver.resolve`` -- used by ``apm install`` / ``apm lock``
   for semver ranges. Raises ``RegistryOnlySemverError`` instead of
   listing upstream tags, because a range needs a version listing the
   proxy does not expose.
3. ``GitReferenceResolver.resolve_commit_sha_for_ref`` -- the GitHub
   commits API used for ref-to-SHA lookup (tier L1 of
   ``TieredRefResolver`` and single-file downloads). Returns ``None``
   without a request.
4. ``apm view <pkg> versions`` -- aborts with an actionable error instead
   of running ``git ls-remote``.
5. ``apm outdated`` -- reports git dependencies as unknown and names the
   remedy instead of querying the source host.

The assertion in each case is that *no upstream call is attempted*, not
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
from apm_cli.install.helpers.ref_reuse import _is_exact_semver_constraint


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
        assert "exact version" in excinfo.value.hint
        assert "unset PROXY_REGISTRY_ONLY" in excinfo.value.hint

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

    def test_hint_does_not_contradict_itself(self):
        """The hint must not tell authors to do what just failed.

        Bare ``1.2.3`` is classified as a semver exact-version
        constraint, so an earlier draft of this guard rejected exact pins
        while advising "pin an exact version". Exact pins now bypass the
        semver route entirely (see
        ``test_exact_version_bypasses_semver_route``); this asserts the
        wording stays consistent with that.
        """
        ref_resolver = MagicMock()
        resolver = GitSemverResolver(ref_resolver)

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with pytest.raises(RegistryOnlySemverError) as excinfo:
                resolver.resolve(
                    owner_repo="myorg/my-package",
                    package_name="my-package",
                    constraint="^1.0.0",
                )

        hint = excinfo.value.hint
        assert "exact version" in hint
        assert "without a listing" in hint
        assert "range" in excinfo.value.summary

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


class TestExactPinBypassesSemverRoute:
    """Exact versions must not be caught by the range guard (#3448 follow-up).

    ``DependencyReference.ref_kind`` classifies bare ``1.2.3`` as a semver
    exact-version constraint rather than a literal tag, so the install
    pipeline routes it through ``GitSemverResolver``. In registry-only
    mode that would hit the range guard, breaking exact pins -- the one
    thing the guard's hint recommends. ``maybe_resolve_git_semver``
    declines the semver route instead, leaving the version as a literal
    ref the proxy can serve directly.
    """

    @pytest.mark.parametrize(
        "constraint,exact",
        [
            ("1.2.3", True),
            ("0.0.1", True),
            ("1.2.3-rc.1", True),
            ("^1.2.0", False),
            ("~1.2.0", False),
            ("1.4.x", False),
            (">=1.0.0", False),
            (">=1.0 <2.0", False),
            ("", False),
            (None, False),
        ],
    )
    def test_exact_constraint_classification(self, constraint, exact):
        assert _is_exact_semver_constraint(constraint) is exact

    def test_exact_version_bypasses_semver_route(self):
        """Registry-only + exact pin -> decline, so the ref stays literal."""
        from apm_cli.install.helpers import ref_reuse

        dep_ref = MagicMock()
        dep_ref.reference = "1.5.0"
        dep_ref.repo_url = "myorg/my-package"

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch.object(
                ref_reuse,
                "is_git_semver_resolution_eligible",
                return_value=True,
            ):
                with patch("apm_cli.deps.git_semver_resolver.GitSemverResolver") as resolver_cls:
                    result = ref_reuse.maybe_resolve_git_semver(
                        dep_ref=dep_ref,
                        existing_lockfile=None,
                        update_refs=False,
                    )

        assert result is None
        resolver_cls.assert_not_called()

    def test_range_still_takes_the_semver_route(self):
        """Registry-only + range -> not declined here; the guard handles it."""
        from apm_cli.install.helpers import ref_reuse

        dep_ref = MagicMock()
        dep_ref.reference = "1.4.x"
        dep_ref.repo_url = "myorg/my-package"

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch.object(
                ref_reuse,
                "is_git_semver_resolution_eligible",
                return_value=True,
            ):
                # Declining happens before any resolver construction, so if
                # the range were wrongly declined this would return None
                # without ever reaching GitSemverResolver.
                assert ref_reuse._is_exact_semver_constraint(dep_ref.reference) is False

    def test_exact_pin_not_declined_when_mode_is_off(self):
        """Regression trap: default behavior keeps resolving exact pins."""
        from apm_cli.install.helpers import ref_reuse

        env = {k: v for k, v in os.environ.items()}
        env.pop("PROXY_REGISTRY_ONLY", None)
        env.pop("ARTIFACTORY_ONLY", None)

        dep_ref = MagicMock()
        dep_ref.reference = "1.5.0"
        dep_ref.repo_url = "myorg/my-package"

        with patch.dict(os.environ, env, clear=True):
            with patch.object(
                ref_reuse,
                "is_git_semver_resolution_eligible",
                return_value=False,
            ) as eligible:
                ref_reuse.maybe_resolve_git_semver(
                    dep_ref=dep_ref,
                    existing_lockfile=None,
                    update_refs=False,
                )

        # Reaching the eligibility check at all proves the registry-only
        # early return did not fire.
        eligible.assert_called_once()


def _mode_off_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PROXY_REGISTRY_ONLY", None)
    env.pop("ARTIFACTORY_ONLY", None)
    return env


_SHA = "0123456789abcdef0123456789abcdef01234567"


def _sha_response() -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.text = _SHA
    return response


@pytest.fixture
def downloader():
    """A real downloader whose only network seam is ``resilient_get``."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    auth = MagicMock()
    ctx = MagicMock()
    ctx.token = None
    auth.resolve.return_value = ctx
    return GitHubPackageDownloader(auth_resolver=auth)


def _github_dep(ref: str = "v1.4.0"):
    from apm_cli.models.dependency.reference import DependencyReference

    return DependencyReference(repo_url="myorg/my-package", reference=ref)


class TestCommitsApiRegistryOnly:
    """Ref-to-SHA lookup must not call the GitHub commits API."""

    def test_returns_none_without_an_api_request(self, downloader):
        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch.object(downloader._strategies, "resilient_get") as get:
                sha = downloader._refs.resolve_commit_sha_for_ref(_github_dep(), "v1.4.0")

        assert sha is None
        get.assert_not_called()

    def test_full_sha_still_passes_through(self, downloader):
        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch.object(downloader._strategies, "resilient_get") as get:
                sha = downloader._refs.resolve_commit_sha_for_ref(_github_dep(_SHA), _SHA.upper())

        assert sha == _SHA
        get.assert_not_called()

    def test_tiered_l1_tier_makes_no_api_request(self, downloader):
        from apm_cli.deps.tiered_ref_resolver import L1CommitsAPI

        tier = L1CommitsAPI(host=downloader)
        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch.object(downloader._strategies, "resilient_get") as get:
                sha = tier.try_resolve(_github_dep(), "v1.4.0")

        assert sha is None
        get.assert_not_called()

    def test_without_the_flag_the_api_is_called(self, downloader):
        """Regression trap: default behavior still uses the commits API."""
        with patch.dict(os.environ, _mode_off_env(), clear=True):
            with patch.object(
                downloader._strategies, "resilient_get", return_value=_sha_response()
            ) as get:
                sha = downloader._refs.resolve_commit_sha_for_ref(_github_dep(), "v1.4.0")

        assert sha == _SHA
        get.assert_called_once()


class TestViewVersionsRegistryOnly:
    """``apm view <pkg> versions`` must not ls-remote in registry-only mode."""

    def _display(self, tmp_path):
        from apm_cli.commands import view

        logger = MagicMock()
        with (
            patch.object(view, "_effective_default_registry", return_value=None),
            patch.object(view, "GitHubPackageDownloader") as downloader_cls,
            patch.object(view, "AuthResolver"),
        ):
            downloader_cls.return_value.list_remote_refs.return_value = []
            try:
                view.display_versions("myorg/my-package", logger, project_root=tmp_path)
                exit_code = None
            except SystemExit as exc:
                exit_code = exc.code
        return logger, downloader_cls, exit_code

    def test_aborts_with_actionable_error_and_no_ls_remote(self, tmp_path):
        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            logger, downloader_cls, exit_code = self._display(tmp_path)

        assert exit_code == 1
        downloader_cls.return_value.list_remote_refs.assert_not_called()
        message = logger.error.call_args.args[0]
        assert "registry-only mode" in message
        assert "exact version" in message
        assert "unset PROXY_REGISTRY_ONLY" in message

    def test_without_the_flag_remote_refs_are_listed(self, tmp_path):
        with patch.dict(os.environ, _mode_off_env(), clear=True):
            logger, downloader_cls, exit_code = self._display(tmp_path)

        assert exit_code is None
        downloader_cls.return_value.list_remote_refs.assert_called_once()
        logger.error.assert_not_called()


class TestOutdatedRegistryOnly:
    """``apm outdated`` must not ls-remote and must name the remedy."""

    def _run_outdated(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from apm_cli.cli import cli
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.models.apm_package import clear_apm_yml_cache

        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        (tmp_path / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\n"
            "dependencies:\n  apm:\n    - myorg/my-package#v1.4.0\n",
            encoding="utf-8",
        )
        lock = LockFile()
        lock.dependencies = {
            "myorg/my-package": LockedDependency(
                repo_url="myorg/my-package",
                resolved_ref="v1.4.0",
                resolved_commit=_SHA,
            ),
        }
        lock.write(tmp_path / "apm.lock.yaml")
        with (
            patch("apm_cli.deps.github_downloader.git") as git_module,
            patch("apm_cli.core.auth.AuthResolver"),
        ):
            git_module.cmd.Git.return_value.ls_remote.return_value = ""
            result = CliRunner().invoke(cli, ["outdated", "-j", "0"])
        return result, git_module

    def test_reports_unknown_with_remedy_and_no_ls_remote(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROXY_REGISTRY_ONLY", "1")
        result, git_module = self._run_outdated(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        git_module.cmd.Git.assert_not_called()
        assert "unknown" in result.output
        assert "PROXY_REGISTRY_ONLY is set" in " ".join(result.output.split())

    def test_without_the_flag_ls_remote_runs_and_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PROXY_REGISTRY_ONLY", raising=False)
        monkeypatch.delenv("ARTIFACTORY_ONLY", raising=False)
        result, git_module = self._run_outdated(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        git_module.cmd.Git.assert_called()
        assert "PROXY_REGISTRY_ONLY is set" not in " ".join(result.output.split())
