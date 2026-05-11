"""Regression tests for round-2 panel findings on PR #941.

Covers the security gates added to ``github_downloader_validation``:

* finding 6 -- ``ls-remote`` no longer fails open; a successful ref
  resolution must be paired with a positive shallow-fetch + ``ls-tree``
  path probe before validation returns ``True``.
* finding 7 -- ``virtual_path`` is screened by
  ``validate_path_segments`` before any URL interpolation, so traversal
  segments cannot leak into Contents-API or archive URLs.
* finding 8 -- Azure DevOps tokens are injected via
  ``http.extraheader`` (``Authorization: Bearer ...``) and never
  embedded in the clone URL or visible on the subprocess argv.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps import github_downloader_validation as gdv
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference


def _make_subdir_dep(
    repo_url: str = "owner/repo",
    vpath: str = "skills/my-skill",
    ref: str | None = "main",
    host: str | None = None,
) -> DependencyReference:
    """Build a virtual-subdirectory ``DependencyReference`` for tests."""
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        reference=ref,
        virtual_path=vpath,
        is_virtual=True,
    )


# ---------------------------------------------------------------------------
# Finding 7: path traversal rejection
# ---------------------------------------------------------------------------


class TestVirtualPathTraversalRejection:
    """``..`` segments in ``virtual_path`` MUST be rejected before any HTTP."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../etc/passwd",
            "skills/../../../secret",
            "..\\windows\\system32",
            "ok/../bad",
        ],
    )
    def test_traversal_segment_rejected_without_network(self, bad_path: str) -> None:
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath=bad_path)

        # Patch download_raw_file to assert it is never reached: validation
        # must fail BEFORE any URL interpolation occurs.
        with patch.object(downloader, "download_raw_file") as raw_mock:
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        raw_mock.assert_not_called()

    def test_clean_path_not_rejected(self) -> None:
        """A normal path falls through to the marker probes (which we mock)."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/clean")

        with patch.object(downloader, "download_raw_file") as raw_mock:
            raw_mock.side_effect = RuntimeError("404")
            with (
                patch.object(gdv, "_directory_exists_at_ref", return_value=False),
                patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(False, None)),
            ):
                ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        # Marker probes ran (proving we got past the path-security gate).
        assert raw_mock.call_count >= 1


# ---------------------------------------------------------------------------
# Finding 6: fail-open close
# ---------------------------------------------------------------------------


class TestLsRemoteFailOpenClose:
    """ls-remote success alone MUST NOT validate a typo'd subdirectory."""

    def _patch_marker_misses(self, downloader: GitHubPackageDownloader):
        """Make every download_raw_file probe miss (404)."""
        return patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404"))

    def test_ls_remote_alone_does_not_validate_when_path_missing(self) -> None:
        """Round-2 finding 6: typo'd vpath after a valid ref must return False.

        Reproduces the security regression: previously, a successful
        ls-remote on the ref bypassed all path validation. Now the
        shallow-fetch + ls-tree probe must also confirm vpath.
        """
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/typo-not-real", ref="main")
        winning = gdv.AttemptSpec("plain HTTPS w/ credential helper", "https://x", {})

        with (
            self._patch_marker_misses(downloader),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(True, winning)),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=False) as path_probe,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False, (
            "Validation must not pass when ls-remote sees the ref but "
            "the subdirectory is absent from the tree."
        )
        path_probe.assert_called_once()
        # Round-3: the winning attempt MUST be threaded through to the
        # tree probe (positional or keyword) -- never attempts[0].
        kwargs = path_probe.call_args.kwargs
        args = path_probe.call_args.args
        assert winning in args or kwargs.get("winning_attempt") is winning

    def test_ls_remote_plus_path_probe_validates(self) -> None:
        """Both gates pass -> validation succeeds, with a deferred-probe warning."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/exists", ref="v1.0.0")
        warnings: list[str] = []
        winning = gdv.AttemptSpec("authenticated HTTPS (header)", "https://x", {})

        with (
            self._patch_marker_misses(downloader),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(True, winning)),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True),
        ):
            ok = gdv.validate_virtual_package_exists(
                downloader, dep_ref, warn_callback=warnings.append
            )

        assert ok is True
        assert len(warnings) == 1, "expected exactly one deferred-probe warning"
        # Warning text must NOT include literal '[!]' (the logger
        # prepends the symbol).
        assert "[!]" not in warnings[0]
        # Round-3: warning must name the dep and use '#' (CLI canonical),
        # not '@' (the version-pin separator from npm/pip/cargo).
        assert "owner/repo" in warnings[0]
        assert "#v1.0.0" in warnings[0]
        assert "@v1.0.0" not in warnings[0]

    def test_ls_remote_only_runs_when_explicit_ref(self) -> None:
        """Without an explicit ``#ref`` the lenient fallback is skipped."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref=None)
        winning = gdv.AttemptSpec("plain HTTPS w/ credential helper", "https://x", {})

        with (
            self._patch_marker_misses(downloader),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(
                gdv, "_ref_exists_via_ls_remote", return_value=(True, winning)
            ) as ls_remote_mock,
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True) as path_mock,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        ls_remote_mock.assert_not_called()
        path_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 8: ADO bearer header injection
# ---------------------------------------------------------------------------


class TestAdoBearerHeaderInjection:
    """ADO tokens must travel via ``http.extraheader``, never the URL."""

    def _make_ado_dep(self) -> DependencyReference:
        return DependencyReference(
            repo_url="myorg/myproj/myrepo",
            host="dev.azure.com",
            reference="main",
            virtual_path="skills/x",
            is_virtual=True,
            ado_organization="myorg",
            ado_project="myproj",
            ado_repo="myrepo",
        )

    def test_ado_basic_pat_injected_as_basic_header_not_url(self) -> None:
        """ADO PAT (auth_scheme=basic) must use Basic base64(:PAT) header."""
        downloader = GitHubPackageDownloader()
        dep_ref = self._make_ado_dep()
        secret = "ADO_PAT_SECRET_VALUE_DO_NOT_LEAK"

        ado_mock_ctx = MagicMock()
        ado_mock_ctx.auth_scheme = "basic"
        ado_mock_ctx.git_env = {}

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=ado_mock_ctx),
            patch.object(
                downloader,
                "_build_repo_url",
                return_value="https://dev.azure.com/myorg/myproj/_git/myrepo",
            ),
            patch.object(
                downloader,
                "_build_noninteractive_git_env",
                return_value={},
            ),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        assert attempts, "expected at least the token attempt"
        labels = [a.label for a in attempts]
        # Round-3 ADO Basic finding: PAT -> Basic header, NOT raw Bearer.
        assert any("basic header" in label.lower() for label in labels), labels

        ado_attempts = [a for a in attempts if "basic header" in a.label.lower()]
        assert len(ado_attempts) == 1
        _label, url, env = ado_attempts[0]

        assert secret not in url, "ADO PAT must not appear in the URL"
        # The env must carry the Basic header.
        assert env.get("GIT_CONFIG_KEY_0") == "http.extraheader"
        header_value = env.get("GIT_CONFIG_VALUE_0", "")
        assert header_value.startswith("Authorization: Basic "), header_value
        # base64(":<PAT>") must contain the expected encoded form.
        import base64

        expected = base64.b64encode(f":{secret}".encode()).decode("ascii")
        assert expected in header_value, "PAT must be base64-encoded as ':<PAT>'"
        # Raw PAT must NOT appear in plaintext anywhere in the env value
        # (only the base64-encoded form is permitted).
        assert secret not in header_value

    def test_ado_bearer_aad_injected_as_bearer_header(self) -> None:
        """ADO + auth_scheme=bearer (AAD JWT) uses raw Bearer header."""
        downloader = GitHubPackageDownloader()
        dep_ref = self._make_ado_dep()
        secret = "fake-aad-jwt-token"

        ado_mock_ctx = MagicMock()
        ado_mock_ctx.auth_scheme = "bearer"
        ado_mock_ctx.git_env = {}

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=ado_mock_ctx),
            patch.object(
                downloader,
                "_build_repo_url",
                return_value="https://dev.azure.com/myorg/myproj/_git/myrepo",
            ),
            patch.object(
                downloader,
                "_build_noninteractive_git_env",
                return_value={},
            ),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        ado_attempts = [a for a in attempts if "bearer header" in a.label.lower()]
        assert len(ado_attempts) == 1
        _label, url, env = ado_attempts[0]
        assert secret not in url
        header_value = env.get("GIT_CONFIG_VALUE_0", "")
        assert header_value == f"Authorization: Bearer {secret}"

    def test_non_ado_token_uses_header_not_url(self) -> None:
        """GitHub deps now also use header injection (round-3 security finding).

        Round-3 closed the gap where non-ADO tokens were embedded in the
        clone URL, leaking via the OS process table and into the temp
        bare repo's .git/config during the shallow-fetch path probe.
        """
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(repo_url="owner/repo", host="github.com")
        secret = "GH_PAT_SECRET"

        captured_token_args: list[str] = []

        def _capture_build_repo_url(*args, **kwargs):
            # Capture the token positional or kwarg so we can assert it
            # is empty for the authenticated attempt.
            captured_token_args.append(kwargs.get("token", ""))
            return "https://github.com/owner/repo.git"

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=None),
            patch.object(downloader, "_build_repo_url", side_effect=_capture_build_repo_url),
            patch.object(
                downloader,
                "_build_noninteractive_git_env",
                return_value={},
            ),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        # The token MUST NOT be passed into the URL builder for the
        # authenticated attempt -- it travels in the env header instead.
        assert all(t == "" for t in captured_token_args), (
            "Round-3 security: non-ADO token must not be embedded in the URL"
        )

        labels = [a.label for a in attempts]
        # Header label, no longer the bare 'authenticated HTTPS'.
        assert any(lbl == "authenticated HTTPS (header)" for lbl in labels), labels

        auth_attempts = [a for a in attempts if a.label == "authenticated HTTPS (header)"]
        assert len(auth_attempts) == 1
        _label, url, env = auth_attempts[0]
        assert secret not in url
        # Header carries the token.
        header_value = env.get("GIT_CONFIG_VALUE_0", "")
        assert header_value == f"Authorization: Bearer {secret}"

    def test_gitlab_pat_injected_as_oauth2_basic_header_not_url(self) -> None:
        """GitLab validation matches oauth2 PAT clone semantics without URL userinfo."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(repo_url="group/repo", host="gitlab.com")
        secret = "glpat_VALIDATION_SECRET"

        mock_ctx = MagicMock()
        mock_ctx.auth_scheme = "basic"
        mock_ctx.git_env = {}

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=mock_ctx),
            patch.object(
                downloader, "_build_repo_url", return_value="https://gitlab.com/group/repo"
            ),
            patch.object(
                downloader,
                "_build_noninteractive_git_env",
                return_value={},
            ),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        gitlab_attempts = [a for a in attempts if "gitlab" in a.label.lower()]
        assert len(gitlab_attempts) == 1
        _label, url, env = gitlab_attempts[0]
        assert secret not in url
        header_value = env.get("GIT_CONFIG_VALUE_0", "")
        assert header_value.startswith("Authorization: Basic "), header_value

        import base64

        expected = base64.b64encode(f"oauth2:{secret}".encode()).decode("ascii")
        assert expected in header_value
        assert secret not in header_value


# ---------------------------------------------------------------------------
# Mechanical guards
# ---------------------------------------------------------------------------


class TestSplitOwnerRepoGuard:
    """Round-2 finding 2: ``repo_url`` without a slash must not raise."""

    def test_returns_none_on_missing_slash(self) -> None:
        assert gdv._split_owner_repo("just-one-segment") is None

    def test_returns_none_on_empty_owner(self) -> None:
        assert gdv._split_owner_repo("/repo") is None

    def test_returns_none_on_empty_repo(self) -> None:
        assert gdv._split_owner_repo("owner/") is None

    def test_returns_pair_for_valid(self) -> None:
        assert gdv._split_owner_repo("owner/repo") == ("owner", "repo")

    def test_directory_probe_returns_false_on_malformed_repo_url(self) -> None:
        """Malformed ``repo_url`` falls through to a clean ``False``."""
        downloader = GitHubPackageDownloader()
        dep_ref = DependencyReference(
            repo_url="malformed-no-slash",
            host="github.com",
            reference="main",
            virtual_path="skills/x",
            is_virtual=True,
        )
        ok = gdv._directory_exists_at_ref(
            downloader, dep_ref, "skills/x", "main", log=lambda _m: None
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Round-3 regression tests
# ---------------------------------------------------------------------------


class TestRound3PathTreeProbeUsesWinningAttempt:
    """Round-3: ``_path_exists_in_tree_at_ref`` must reuse the winning attempt.

    Previously it used ``attempts[0]`` unconditionally, breaking the
    auth-chain promise when ls-remote succeeded via SSH or plain HTTPS
    but the leading PAT attempt would fail the shallow fetch.
    """

    @pytest.mark.parametrize(
        "winning_label,winning_url",
        [
            ("SSH", "git@github.com:owner/repo.git"),
            ("plain HTTPS w/ credential helper", "https://github.com/owner/repo.git"),
        ],
    )
    def test_tree_probe_uses_winning_attempt_not_attempts_zero(
        self, winning_label: str, winning_url: str
    ) -> None:
        """The shallow-fetch must use the URL/env from the winning ls-remote attempt."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref="main")
        winning_env = {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"} if "SSH" in winning_label else {}
        winning = gdv.AttemptSpec(winning_label, winning_url, winning_env)

        with (
            patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(True, winning)),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True) as path_probe,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is True
        path_probe.assert_called_once()
        # The winning AttemptSpec must be passed positionally or by kw.
        args = path_probe.call_args.args
        kwargs = path_probe.call_args.kwargs
        assert winning in args or kwargs.get("winning_attempt") is winning, (
            "Path probe must receive the winning attempt -- not attempts[0]."
        )


class TestRound3NonAdoTokenNotInProcessArgv:
    """Round-3: non-ADO HTTPS token MUST NOT appear in subprocess argv."""

    def test_non_ado_token_not_in_url_or_argv(self) -> None:
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(repo_url="owner/repo", host="github.com")
        secret = "GH_PAT_NEVER_IN_ARGV"

        with (
            patch.object(downloader, "_resolve_dep_token", return_value=secret),
            patch.object(downloader, "_resolve_dep_auth_ctx", return_value=None),
            patch.object(
                downloader,
                "_build_repo_url",
                return_value="https://github.com/owner/repo.git",
            ),
            patch.object(downloader, "_build_noninteractive_git_env", return_value={}),
        ):
            attempts = gdv._build_validation_attempts(downloader, dep_ref, log=lambda _m: None)

        for attempt in attempts:
            assert secret not in attempt.url, f"Token leaked into URL for attempt '{attempt.label}'"
        # Header injection: the auth attempt env must contain a Bearer header.
        auth_attempt = next(a for a in attempts if "header" in a.label)
        assert auth_attempt.env.get("GIT_CONFIG_KEY_0") == "http.extraheader"
        assert auth_attempt.env["GIT_CONFIG_VALUE_0"] == f"Authorization: Bearer {secret}"


class TestRound3SafeRmtreeNotRobustRmtreeDirect:
    """Round-3: cleanup MUST go through safe_rmtree (containment gate)."""

    def test_safe_rmtree_called_not_robust_rmtree_direct(self) -> None:
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref="main")
        winning = gdv.AttemptSpec("plain HTTPS w/ credential helper", "https://x", {})

        # Patch git.cmd.Git so init/fetch/ls_tree don't actually run.
        with (
            patch("apm_cli.deps.github_downloader_validation.safe_rmtree") as safe_rm_mock,
            patch("apm_cli.deps.github_downloader_validation.git.cmd.Git") as MockGit,
        ):
            MockGit.return_value.init = MagicMock()
            MockGit.return_value.remote = MagicMock()
            MockGit.return_value.fetch = MagicMock()
            MockGit.return_value.ls_tree = MagicMock(return_value="100644 blob abc\tskills/x")
            ok = gdv._path_exists_in_tree_at_ref(
                downloader,
                dep_ref,
                "skills/x",
                "main",
                log=lambda _m: None,
                winning_attempt=winning,
            )

        assert ok is True
        safe_rm_mock.assert_called_once()
        # Containment: first arg is the tmpdir, second is the base.
        call_args = safe_rm_mock.call_args.args
        assert len(call_args) == 2, "safe_rmtree must be called with (path, base_dir)"


class TestRound3WarnMessage:
    """Round-3: warn message names dep with '#' separator and is verbose-gated."""

    def test_warn_message_uses_hash_separator_and_names_dep(self) -> None:
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/cool", ref="v2.0.0")
        warnings: list[str] = []
        winning = gdv.AttemptSpec("authenticated HTTPS (header)", "https://x", {})

        with (
            patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote", return_value=(True, winning)),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True),
        ):
            ok = gdv.validate_virtual_package_exists(
                downloader, dep_ref, warn_callback=warnings.append
            )

        assert ok is True
        assert len(warnings) == 1
        msg = warnings[0]
        # Names the dep (canonical form).
        assert "owner/repo" in msg
        assert "skills/cool" in msg
        # Uses '#' (CLI canonical), not '@' (the npm/pip version-pin separator).
        assert "#v2.0.0" in msg
        assert "@v2.0.0" not in msg
        # Avoids the bogus 'Contents-API scope' jargon flagged by growth.
        assert "Contents-API scope" not in msg


class TestRound4WarnSurfacedOnHappyPath:
    """Round-4 (cli-logging + devx-ux): the git-fallback warning is yellow.

    The remote panel rejected round-3's suppression-on-happy-path: a
    scoped PAT may have correctly rejected a package on the API surface
    while the git credential chain accepted it; operators MUST see that
    in default CI logs. Round-4 surfaces the warning in both verbose
    and non-verbose modes, and strips the "Run with --verbose for
    details." suffix only when --verbose is already set.
    """

    def _patch_validate(self, msg: str):
        """Patch the downloader to invoke warn_callback once with ``msg``."""

        def fake_validate(self, dep_ref, verbose_callback=None, warn_callback=None):
            if warn_callback is not None:
                warn_callback(msg)
            return True

        return patch(
            "apm_cli.deps.github_downloader.GitHubPackageDownloader."
            "validate_virtual_package_exists",
            new=fake_validate,
        )

    def _stub_resolver(self) -> MagicMock:
        auth_resolver = MagicMock()
        ctx = MagicMock()
        ctx.source = "env"
        ctx.token_type = "pat"
        auth_resolver.resolve_for_dep.return_value = ctx
        return auth_resolver

    def test_warn_emits_in_non_verbose_mode_with_suffix_kept(self) -> None:
        from apm_cli.install import validation as install_validation

        logger = MagicMock()
        msg = (
            "API validation skipped for owner/repo/sub#v1; "
            "resolved via git credential fallback. "
            "Run with --verbose for details."
        )
        with self._patch_validate(msg):
            ok = install_validation._validate_package_exists(
                "owner/repo/sub#v1",
                verbose=False,
                auth_resolver=self._stub_resolver(),
                logger=logger,
            )
        assert ok is True
        # Yellow signal must reach the user even in default-verbosity.
        logger.warning.assert_called_once()
        # Suffix is kept in non-verbose so the user knows --verbose digs deeper.
        assert "Run with --verbose for details." in logger.warning.call_args[0][0]

    def test_warn_emits_in_verbose_mode_with_suffix_stripped(self) -> None:
        from apm_cli.install import validation as install_validation

        logger = MagicMock()
        msg = (
            "API validation skipped for owner/repo/sub#v1; "
            "resolved via git credential fallback. "
            "Run with --verbose for details."
        )
        with self._patch_validate(msg):
            ok = install_validation._validate_package_exists(
                "owner/repo/sub#v1",
                verbose=True,
                auth_resolver=self._stub_resolver(),
                logger=logger,
            )
        assert ok is True
        logger.warning.assert_called_once()
        # In verbose mode, the suffix becomes a no-op: strip it so output
        # is not self-referential.
        emitted = logger.warning.call_args[0][0]
        assert "Run with --verbose for details." not in emitted
        assert "API validation skipped for owner/repo/sub#v1" in emitted

    def test_warn_falls_back_to_rich_warning_when_logger_is_none(self) -> None:
        """No-logger production callers must still emit the yellow signal.

        Round-3 left a comment claiming "logger is always present in the
        install code path"; round-4 enforces it via a ``_rich_warning``
        fallback so silent-drop is impossible regardless of caller wiring.
        """
        from apm_cli.install import validation as install_validation

        msg = (
            "API validation skipped for owner/repo/sub#v1; "
            "resolved via git credential fallback. "
            "Run with --verbose for details."
        )
        with (
            self._patch_validate(msg),
            patch("apm_cli.install.validation._rich_warning") as rich,
        ):
            ok = install_validation._validate_package_exists(
                "owner/repo/sub#v1",
                verbose=False,
                auth_resolver=self._stub_resolver(),
                logger=None,
            )
        assert ok is True
        rich.assert_called_once()
        assert "API validation skipped" in rich.call_args[0][0]


class TestRound4EmptyRefAndEmptyVpathGates:
    """Round-4 supply-chain: bare `#` and empty vpath must NOT bypass gates."""

    def test_empty_string_ref_does_not_activate_git_fallback(self) -> None:
        """A bare ``#`` fragment yields ``reference=""``; the git fallback
        must remain unreachable. Round-3 used ``is not None`` and let
        empty string through, contradicting the documented invariant
        that the fallback is only reachable for explicitly-pinned refs.
        """
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref="")

        with (
            patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(gdv, "_ref_exists_via_ls_remote") as ls_remote,
            patch.object(gdv, "_path_exists_in_tree_at_ref") as ls_tree,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        ls_remote.assert_not_called()
        ls_tree.assert_not_called()

    def test_empty_vpath_rejected_before_any_network(self) -> None:
        """Empty ``virtual_path`` must be rejected at the entry point.

        ``git ls-tree FETCH_HEAD ""`` is implementation-defined: some
        git versions root-list the tree, which would falsely validate
        any successfully-fetched repo. ``reject_empty=True`` on the
        ``validate_path_segments`` call closes that hole before any
        network or git operation runs.
        """
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="", ref="v1.0.0")

        with (
            patch.object(downloader, "download_raw_file") as raw_mock,
            patch.object(gdv, "_directory_exists_at_ref") as dir_mock,
            patch.object(gdv, "_ref_exists_via_ls_remote") as ls_remote,
            patch.object(gdv, "_path_exists_in_tree_at_ref") as ls_tree,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is False
        raw_mock.assert_not_called()
        dir_mock.assert_not_called()
        ls_remote.assert_not_called()
        ls_tree.assert_not_called()

    def test_explicit_ref_still_activates_git_fallback(self) -> None:
        """Sanity: a real ref pin (``v1.0.0``) MUST still reach the
        git fallback. Guards against an over-eager fix that would also
        block legitimate explicit-ref flows.
        """
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="skills/x", ref="v1.0.0")
        winning = gdv.AttemptSpec("authenticated HTTPS (header)", "https://x", {})

        with (
            patch.object(downloader, "download_raw_file", side_effect=RuntimeError("404")),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(
                gdv, "_ref_exists_via_ls_remote", return_value=(True, winning)
            ) as ls_remote,
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True) as ls_tree,
        ):
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is True
        ls_remote.assert_called_once()
        ls_tree.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #1094: probe order in SUBDIRECTORY case
# ---------------------------------------------------------------------------


class TestSubdirectoryProbeOrder:
    """``apm.yml`` is probed first for SUBDIRECTORY paths.

    The legacy ``/collections/`` path heuristic is removed: a path like
    ``collections/foo`` is now classified SUBDIRECTORY and resolved at fetch
    time. ``apm.yml`` is the supported way to express a curated dependency
    aggregator (#1094); the ``.collection.yml`` form was removed.
    """

    def test_apm_yml_at_collections_path_short_circuits_collection_probe(self) -> None:
        """`<vpath>/apm.yml` hits first; subdirectory probe stops there."""
        downloader = GitHubPackageDownloader()
        dep_ref = _make_subdir_dep(vpath="collections/writing", ref="main")

        # Whitelist only `<vpath>/apm.yml`; everything else 404s.
        def fake_download(_dr, path, _ref):
            if path == "collections/writing/apm.yml":
                return b"name: writing\nversion: 1.0.0\n"
            raise RuntimeError("404")

        with patch.object(downloader, "download_raw_file", side_effect=fake_download) as raw:
            ok = gdv.validate_virtual_package_exists(downloader, dep_ref)

        assert ok is True
        attempted_paths = [call.args[1] for call in raw.call_args_list]
        assert "collections/writing/apm.yml" in attempted_paths
