"""The resolver answers only the anonymous public github.com attempt in process."""

from __future__ import annotations

import subprocess
import types
from unittest.mock import patch

import pytest

from apm_cli.deps.git_reference_resolver import GitReferenceResolver
from tests.integration.test_git_reference_resolver import _ctx, _dep

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-git-transport-selection", "P-OWN-host-credential-resolution"),
]


class TestAnonymousInProcessListing:
    """The anonymous public github.com attempt may skip the git child, nothing else."""

    LISTED = "ccc3333333333333333333333333333333333333\trefs/tags/v2.0.0\n"
    GIT = "ddd4444444444444444444444444444444444444\trefs/tags/v3.0.0\n"

    def _run(self, *, token: str | None, listed: str | None):
        env = {"GIT_TERMINAL_PROMPT": "0"}
        host = _ctx()
        host.auth_resolver.uses_public_github_anonymous_first.return_value = True
        host.auth_resolver.try_with_fallback.side_effect = lambda _host, operation, **_kw: (
            operation(token, env)
        )
        host._build_repo_url.return_value = "https://github.com/owner/repo.git"
        git_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=self.GIT, stderr="")
        with (
            patch("apm_cli.deps.github_downloader.git", types.SimpleNamespace()),
            patch(
                "apm_cli.deps.anonymous_ref_listing.list_refs_anonymously", return_value=listed
            ) as in_process,
            patch("apm_cli.utils.git_env.git_remote_refs", return_value=git_result) as spawned,
        ):
            refs = GitReferenceResolver(host).list_remote_refs(_dep(host="github.com"))
        return [r.name for r in refs], in_process, spawned, env

    def test_listed_refs_skip_the_git_child(self):
        names, in_process, spawned, env = self._run(token=None, listed=self.LISTED)

        assert names == ["refs/tags/v2.0.0"]
        in_process.assert_called_once_with(
            "https://github.com/owner/repo.git", include_heads=True, env=env
        )
        spawned.assert_not_called()

    def test_unproven_listing_runs_git_with_the_same_env(self):
        names, in_process, spawned, env = self._run(token=None, listed=None)

        assert names == ["refs/tags/v3.0.0"]
        in_process.assert_called_once()
        assert spawned.call_args.kwargs["env"] is env

    def test_token_attempts_never_list_in_process(self):
        names, in_process, spawned, _env = self._run(token="ghp_secret", listed=self.LISTED)

        assert names == ["refs/tags/v3.0.0"]
        in_process.assert_not_called()
        spawned.assert_called_once()
