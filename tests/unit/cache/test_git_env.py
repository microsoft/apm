"""Tests for git subprocess environment sanitization."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest

from apm_cli.utils.git_env import (
    _STRIP_GIT_VARS,
    GitConfigEntry,
    GitUrlRewriteError,
    GitUrlRewriteProbeError,
    _GitConfigSnapshot,
    _resolve_trusted_executable,
    clone_git_worktree,
    get_gh_executable,
    get_git_executable,
    git_network_env,
    git_remote_refs,
    git_subprocess_env,
    git_subprocess_error_text,
    reset_git_cache,
    set_git_authorization_header,
)

# Entire module: this is the canonical owner of resolved,
# PATH-independent git executable lookup (microsoft/apm#2233's bare
# ["git", ...] argv WinError 2 class). Selected by the PR-time Windows
# Compatibility Gate via `pytest -m windows_compat`; also runs on
# every other OS.
pytestmark = pytest.mark.windows_compat
_REAL_SUBPROCESS_RUN = subprocess.run


def _run_real_git_config_and_fake_clone(args, **kwargs):
    """Use real local config parsing while preventing any clone network I/O."""
    if len(args) > 1 and args[1] == "config":
        return _REAL_SUBPROCESS_RUN(args, **kwargs)
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TestGetGitExecutable:
    """Test cached git binary lookup."""

    def setup_method(self) -> None:
        reset_git_cache()

    def teardown_method(self) -> None:
        reset_git_cache()

    @patch(
        "apm_cli.utils.git_env._resolve_trusted_executable",
        return_value="/usr/bin/git",
    )
    def test_returns_git_path(self, mock_resolve) -> None:
        result = get_git_executable()
        assert result == "/usr/bin/git"
        mock_resolve.assert_called_once_with("git")

    @patch(
        "apm_cli.utils.git_env._resolve_trusted_executable",
        return_value="/usr/bin/git",
    )
    def test_cached_after_first_call(self, mock_resolve) -> None:
        """Resolution runs only once across multiple invocations."""
        get_git_executable()
        get_git_executable()
        get_git_executable()
        mock_resolve.assert_called_once()

    @patch(
        "apm_cli.utils.git_env._resolve_trusted_executable",
        side_effect=FileNotFoundError,
    )
    def test_raises_if_git_not_found(self, mock_resolve) -> None:
        with pytest.raises(FileNotFoundError, match=r"git executable not found"):
            get_git_executable()

    @patch(
        "apm_cli.utils.git_env._resolve_trusted_executable",
        side_effect=[FileNotFoundError, "/usr/bin/git"],
    )
    def test_transient_failure_does_not_poison_later_resolution(self, mock_resolve) -> None:
        """A transient PATH miss raises but remains retryable."""
        with pytest.raises(FileNotFoundError):
            get_git_executable()

        assert get_git_executable() == "/usr/bin/git"
        assert mock_resolve.call_count == 2


class TestGetGhExecutable:
    """Test cached GitHub CLI binary lookup."""

    def setup_method(self) -> None:
        reset_git_cache()

    def teardown_method(self) -> None:
        reset_git_cache()

    @patch(
        "apm_cli.utils.git_env._resolve_trusted_executable",
        side_effect=FileNotFoundError,
    )
    def test_missing_gh_has_actionable_error(self, mock_resolve) -> None:
        with pytest.raises(FileNotFoundError, match=r"Please install it"):
            get_gh_executable()


class TestResolveTrustedExecutable:
    """Test exclusion of executable candidates controlled by the project."""

    def test_skips_path_directories_inside_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        project_bin = project / "bin"
        nested_cwd = project / "packages" / "example"
        trusted_bin = tmp_path / "tools"
        (project / ".git").mkdir(parents=True)
        project_bin.mkdir(parents=True)
        nested_cwd.mkdir(parents=True)
        trusted_bin.mkdir()
        monkeypatch.chdir(nested_cwd)

        with (
            patch("os.get_exec_path", return_value=[str(project_bin), str(trusted_bin)]),
            patch("shutil.which", return_value=str(trusted_bin / "git")) as mock_which,
        ):
            result = _resolve_trusted_executable("git")

        assert result == str((trusted_bin / "git").resolve())
        mock_which.assert_called_once_with(str(trusted_bin / "git"))

    def test_rejects_candidate_resolving_inside_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        trusted_bin = tmp_path / "tools"
        (project / ".git").mkdir(parents=True)
        trusted_bin.mkdir()
        monkeypatch.chdir(project)

        with (
            patch("os.get_exec_path", return_value=[str(trusted_bin)]),
            patch("shutil.which", return_value=str(project / "git")),
            pytest.raises(FileNotFoundError),
        ):
            _resolve_trusted_executable("git")


class TestGitSubprocessEnv:
    """Test environment sanitization."""

    def test_strips_git_dir(self) -> None:
        with patch.dict(os.environ, {"GIT_DIR": "/some/path/.git"}):
            env = git_subprocess_env()
            assert "GIT_DIR" not in env

    def test_strips_git_work_tree(self) -> None:
        with patch.dict(os.environ, {"GIT_WORK_TREE": "/some/path"}):
            env = git_subprocess_env()
            assert "GIT_WORK_TREE" not in env

    def test_strips_git_index_file(self) -> None:
        with patch.dict(os.environ, {"GIT_INDEX_FILE": "/tmp/index"}):
            env = git_subprocess_env()
            assert "GIT_INDEX_FILE" not in env

    def test_strips_all_ambient_vars(self) -> None:
        env_override = {var: "value" for var in _STRIP_GIT_VARS}
        with patch.dict(os.environ, env_override):
            env = git_subprocess_env()
            for var in _STRIP_GIT_VARS:
                assert var not in env

    def test_strips_repository_local_behavior_vars(self) -> None:
        local_state = {
            "GIT_GRAFT_FILE": "/repo/info/grafts",
            "GIT_IMPLICIT_WORK_TREE": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PREFIX": "nested/",
        }
        with patch.dict(os.environ, local_state):
            env = git_subprocess_env()
        assert not local_state.keys() & env.keys()

    def test_preserves_git_ssh_command(self) -> None:
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i ~/.ssh/id_rsa"}):
            env = git_subprocess_env()
            assert env["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/id_rsa"

    def test_preserves_git_config_global(self) -> None:
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/etc/gitconfig"}):
            env = git_subprocess_env()
            assert env["GIT_CONFIG_GLOBAL"] == "/etc/gitconfig"

    def test_preserves_https_proxy(self) -> None:
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.corp:8080"}):
            env = git_subprocess_env()
            assert env["HTTPS_PROXY"] == "http://proxy.corp:8080"

    def test_preserves_ssh_askpass(self) -> None:
        with patch.dict(os.environ, {"SSH_ASKPASS": "/usr/lib/ssh/ssh-askpass"}):
            env = git_subprocess_env()
            assert env["SSH_ASKPASS"] == "/usr/lib/ssh/ssh-askpass"

    def test_preserves_git_terminal_prompt(self) -> None:
        with patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "0"}):
            env = git_subprocess_env()
            assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_preserves_regular_env_vars(self) -> None:
        with patch.dict(os.environ, {"HOME": "/home/user", "PATH": "/usr/bin"}):
            env = git_subprocess_env()
            assert env["HOME"] == "/home/user"
            assert env["PATH"] == "/usr/bin"

    def test_strips_pyinstaller_ld_library_path_when_frozen(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle/internal"}, clear=True),
        ):
            env = git_subprocess_env()
            assert "LD_LIBRARY_PATH" not in env

    def test_restores_original_ld_library_path_when_frozen(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(
                os.environ,
                {
                    "LD_LIBRARY_PATH": "/bundle/internal",
                    "LD_LIBRARY_PATH_ORIG": "/custom/lib",
                },
                clear=True,
            ),
        ):
            env = git_subprocess_env()
            assert env["LD_LIBRARY_PATH"] == "/custom/lib"
            assert "LD_LIBRARY_PATH_ORIG" not in env

    def test_preserves_ld_library_path_when_not_frozen(self) -> None:
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/custom/lib"}, clear=True):
            env = git_subprocess_env()
            assert env["LD_LIBRARY_PATH"] == "/custom/lib"

    def test_subprocess_error_text_prefers_captured_stderr(self) -> None:
        exc = subprocess.CalledProcessError(
            128,
            ["git", "fetch"],
            output=b"fallback output",
            stderr=b"fatal: missing ref\n",
        )
        assert git_subprocess_error_text(exc) == "fatal: missing ref"

    def test_subprocess_environment_forces_trace_redaction(self) -> None:
        with patch.dict(os.environ, {"GIT_TRACE_REDACT": "0"}, clear=True):
            env = git_subprocess_env()

        assert env["GIT_TRACE_REDACT"] == "1"

    def test_subprocess_error_text_redacts_authorization_header(self) -> None:
        exc = subprocess.CalledProcessError(
            128,
            ["git", "fetch"],
            stderr="trace: Authorization: Basic secret-value\nfatal: denied",
        )

        message = git_subprocess_error_text(exc)

        assert "Authorization: ******" in message
        assert "secret-value" not in message

    @pytest.mark.parametrize(
        "secret",
        (
            "github_pat_" + "A" * 30,
            "ghp_" + "B" * 36,
            "glpat-" + "C" * 24,
            "glrt-" + "R" * 24,
            "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20,
            "D" * 75 + "AZDO" + "E" * 5,
            "F" * 52,
        ),
        ids=(
            "github-fine-grained",
            "github-classic",
            "gitlab-pat",
            "gitlab-runner",
            "aad-jwt",
            "ado-new",
            "ado-legacy",
        ),
    )
    def test_subprocess_error_text_redacts_bare_platform_tokens(self, secret: str) -> None:
        exc = subprocess.CalledProcessError(
            128,
            ["git", "fetch"],
            stderr=f"remote: rejected credential {secret}\nfatal: denied",
        )

        message = git_subprocess_error_text(exc)

        assert secret not in message
        assert "***" in message

    def test_subprocess_error_text_redacts_private_key_path_but_keeps_ssh_cause(self) -> None:
        exc = subprocess.CalledProcessError(
            128,
            ["git", "clone"],
            stderr=(
                "Enter passphrase for key '/Users/alice/.ssh/id_private':\n"
                "Host key verification failed.\n"
            ),
        )

        message = git_subprocess_error_text(exc)

        assert "/Users/alice/.ssh/id_private" not in message
        assert "Host key verification failed." in message

    def test_rewrite_probe_failure_has_safe_recovery(self) -> None:
        result = subprocess.CompletedProcess(
            ["git", "config"],
            2,
            stdout=b"",
            stderr=b"private config detail",
        )
        with (
            patch("apm_cli.utils.git_env._git_config_run", return_value=result),
            pytest.raises(GitUrlRewriteProbeError) as raised,
        ):
            git_network_env("https://git.example.com/acme/repo")

        message = str(raised.value)
        assert "Git config probe failed" in message
        assert "check Git configuration and retry" in message
        assert "--show-origin" in message
        assert "private config detail" not in message

    def test_rewrite_probe_retries_once_after_timeout(self) -> None:
        calls: list[int] = []

        def fake_probe(args, **kwargs):
            calls.append(kwargs["timeout"])
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with patch("apm_cli.utils.git_env._git_config_run", side_effect=fake_probe):
            env = git_network_env("https://git.example.com/acme/repo")

        assert env["GIT_TRACE_REDACT"] == "1"
        assert calls == [10, 30]

    def test_rewrite_probe_still_fails_closed_after_retry_timeouts(self) -> None:
        calls: list[int] = []

        def fake_probe(args, **kwargs):
            calls.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        with (
            patch("apm_cli.utils.git_env._git_config_run", side_effect=fake_probe),
            pytest.raises(GitUrlRewriteProbeError) as raised,
        ):
            git_network_env("https://git.example.com/acme/repo")

        message = str(raised.value)
        assert "Git config probe timed out" in message
        assert "check Git configuration and retry" in message
        assert calls == [10, 30]

    def test_clone_retains_url_rewrite_without_restoring_parent_auth(self, tmp_path) -> None:
        config = tmp_path / "gitconfig"
        config.write_text("", encoding="ascii")
        parent = {
            "PATH": os.environ["PATH"],
            "GITHUB_TOKEN": "ambient-token",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_HTTP_EXTRAHEADER": "Authorization: Basic stale",
            "GIT_CONFIG_PARAMETERS": "'http.extraheader=Authorization: Basic stale'",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic stale",
            "GIT_CONFIG_KEY_1": "url.file:///fixture/.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://git.example.com/acme/repo",
        }
        resolved = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
        }
        with (
            patch.dict(os.environ, parent, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=resolved,
            )

        child = run.call_args_list[-1].kwargs["env"]
        assert "GITHUB_TOKEN" not in child
        assert "GIT_HTTP_EXTRAHEADER" not in child
        assert "GIT_CONFIG_PARAMETERS" not in child
        assert child["GIT_CONFIG_GLOBAL"] == os.devnull
        entries = {
            (
                child.get(f"GIT_CONFIG_KEY_{index}", ""),
                child.get(f"GIT_CONFIG_VALUE_{index}", ""),
            )
            for index in range(int(child["GIT_CONFIG_COUNT"]))
        }
        assert (
            "url.file:///fixture/.insteadof",
            "https://git.example.com/acme/repo",
        ) in entries
        assert all(not value.lower().startswith("authorization:") for _, value in entries)

    @pytest.mark.parametrize(
        ("replacement", "message"),
        (
            ("https://token@example.com/repo", "must not contain credentials"),
            ("http://example.com/repo", "must not rewrite to insecure HTTP"),
        ),
    )
    def test_clone_rejects_unsafe_parent_url_rewrites(
        self, tmp_path, replacement: str, message: str
    ) -> None:
        parent = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{replacement}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://git.example.com/acme/repo",
        }
        with (
            patch.dict(os.environ, parent, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
            pytest.raises(ValueError, match=message),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env={"PATH": os.environ.get("PATH", "")},
            )
        run.assert_not_called()

    @pytest.mark.parametrize("source", ("inherited", "existing"))
    @pytest.mark.parametrize(
        ("replacement", "message"),
        (
            ("https://token@example.com/repo", "must not contain credentials"),
            ("http://example.com/repo", "must not rewrite to insecure HTTP"),
        ),
    )
    def test_clone_rejects_unsafe_effective_url_rewrites(
        self,
        tmp_path,
        source: str,
        replacement: str,
        message: str,
    ) -> None:
        rewrite_env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{replacement}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://git.example.com/acme/repo",
        }
        parent = rewrite_env if source == "inherited" else {"PATH": os.environ["PATH"]}
        supplied = None if source == "inherited" else rewrite_env
        with (
            patch.dict(os.environ, parent, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
            pytest.raises(ValueError, match=message),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=supplied,
            )

        run.assert_not_called()

    @pytest.mark.parametrize(
        ("replacement", "message"),
        (
            ("https://token@example.com/repo", "must not contain credentials"),
            ("http://example.com/repo", "must not rewrite to insecure HTTP"),
        ),
    )
    def test_clone_rejects_unsafe_url_rewrite_after_safe_index(
        self,
        tmp_path,
        replacement: str,
        message: str,
    ) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "url.file:///safe/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://unrelated.example/repo",
            "GIT_CONFIG_KEY_1": f"url.{replacement}.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://git.example.com/acme/repo",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
            pytest.raises(GitUrlRewriteError, match=message),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        run.assert_not_called()

    def test_clone_rejects_authenticated_cross_origin_https_rewrite(self, tmp_path) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic sentinel",
            "GIT_CONFIG_KEY_1": "url.https://mirror.example/repo.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://git.example.com/acme/repo",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match="different HTTPS origin"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

    def test_clone_rejects_cross_origin_rewrite_with_config_credential_header(
        self,
        tmp_path,
    ) -> None:
        config = tmp_path / "gitconfig"
        config.write_text(
            '[url "https://mirror.example/"]\n'
            "\tinsteadOf = https://git.example.com/\n"
            "[http]\n"
            "\textraHeader = Cookie: session=sentinel\n",
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match="different HTTPS origin"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

    def test_clone_allows_port_rewrite_with_unrelated_scoped_header(
        self,
        tmp_path,
    ) -> None:
        config = tmp_path / "gitconfig"
        config.write_text(
            '[url "https://git.example.com:8443/"]\n'
            "\tinsteadOf = https://git.example.com/\n"
            '[http "https://unrelated.example/"]\n'
            "\textraHeader = Authorization: Basic unrelated\n",
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        assert "clone" in run.call_args_list[-1].args[0]

    @pytest.mark.parametrize("specific_first", (True, False))
    def test_specific_empty_header_overrides_global_authorization(
        self,
        tmp_path,
        specific_first: bool,
    ) -> None:
        specific = '[http "https://git.example.com:8443/"]\n\textraHeader =\n'
        global_header = "[http]\n\textraHeader = Authorization: Basic sentinel\n"
        config = tmp_path / "gitconfig"
        config.write_text(
            '[url "https://git.example.com:8443/"]\n'
            "\tinsteadOf = https://git.example.com/\n"
            f"{specific if specific_first else global_header}"
            f"{global_header if specific_first else specific}",
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        assert "clone" in run.call_args_list[-1].args[0]

    def test_managed_auth_rejects_port_rewrite_masked_by_empty_target_header(
        self,
        tmp_path,
    ) -> None:
        config = tmp_path / "gitconfig"
        config.write_text(
            '[url "https://git.example.com:8443/"]\n'
            "\tinsteadOf = https://git.example.com/\n"
            '[http "https://git.example.com:8443/"]\n'
            "\textraHeader =\n",
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        set_git_authorization_header(env, "Basic", "managed-sentinel")

        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match="different HTTPS origin"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

    @pytest.mark.parametrize(
        ("key", "value"),
        (
            (
                "GIT_HTTP_EXTRAHEADER",
                "X-Apm-Safe: value\r\nAuthorization: Basic injected-secret",
            ),
            (
                "GIT_CONFIG_VALUE_0",
                "X-Apm-Safe: value\nAuthorization: Basic injected-secret",
            ),
        ),
        ids=("direct-header-env", "indexed-config"),
    )
    def test_network_env_drops_header_delimiter_injection(
        self,
        key: str,
        value: str,
    ) -> None:
        env = {"PATH": os.environ["PATH"], key: value}
        if key == "GIT_CONFIG_VALUE_0":
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraheader",
                }
            )

        child = git_network_env("https://git.example.com/acme/repo", env)

        assert child.get("GIT_HTTP_EXTRAHEADER") is None
        assert all(
            "injected-secret" not in config_value
            for config_key, config_value in child.items()
            if config_key.startswith("GIT_CONFIG_VALUE_")
        )

    @pytest.mark.parametrize("specific_first", (True, False))
    def test_specific_authorization_overrides_empty_global_header(
        self,
        tmp_path,
        specific_first: bool,
    ) -> None:
        specific = '[http "https://git.example.com:8443/"]\n\textraHeader = Authorization: Basic sentinel\n'
        global_reset = "[http]\n\textraHeader =\n"
        config = tmp_path / "gitconfig"
        config.write_text(
            '[url "https://git.example.com:8443/"]\n'
            "\tinsteadOf = https://git.example.com/\n"
            f"{specific if specific_first else global_reset}"
            f"{global_reset if specific_first else specific}",
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match="different HTTPS origin"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

    def test_clone_rejects_rewrite_of_credential_bearing_remote(self, tmp_path) -> None:
        token = "source-token-sentinel"
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.ssh://git@mirror.example/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError) as raised,
        ):
            clone_git_worktree(
                f"https://{token}@git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        assert token not in str(raised.value)

    def test_clone_allows_ssh_username_rewritten_to_file(self, tmp_path) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.file:///fixture/.insteadOf",
            "GIT_CONFIG_VALUE_0": "ssh://git@git.example.com/",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "ssh://git@git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        assert "clone" in run.call_args_list[-1].args[0]

    def test_clone_rejects_ssh_password_in_rewritten_remote(self, tmp_path) -> None:
        secret = "ssh-password-sentinel"
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.file:///fixture/.insteadOf",
            "GIT_CONFIG_VALUE_0": "ssh://",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError) as raised,
        ):
            clone_git_worktree(
                f"ssh://git:{secret}@git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        assert secret not in str(raised.value)

    @pytest.mark.parametrize(
        "replacement",
        (
            "https://mirror.example/",
            "ssh://git@mirror.example/",
            "git@mirror.example:",
        ),
    )
    def test_clone_rejects_cross_host_network_rewrite(
        self,
        tmp_path,
        replacement: str,
    ) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{replacement}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://git.example.com/",
        }

        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
            pytest.raises(GitUrlRewriteError, match="different network host"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        run.assert_not_called()

    def test_clone_rejects_authorized_ssh_to_https_rewrite(self, tmp_path) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic sentinel",
            "GIT_CONFIG_KEY_1": "url.https://mirror.example/.insteadOf",
            "GIT_CONFIG_VALUE_1": "ssh://git@git.example.com/",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match="different HTTPS origin"),
        ):
            clone_git_worktree(
                "ssh://git@git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

    def test_clone_rejects_ssh_remote_rewritten_to_http(self, tmp_path) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic sentinel",
            "GIT_CONFIG_KEY_1": "url.http://mirror.example/.insteadOf",
            "GIT_CONFIG_VALUE_1": "git@git.example.com:",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match="insecure HTTP"),
        ):
            clone_git_worktree(
                "git@git.example.com:acme/repo",
                tmp_path / "clone",
                env=env,
            )

    @pytest.mark.parametrize(
        ("replacement", "message"),
        (
            ("git", "insecure transport"),
            ("ext::helper", "remote-helper syntax"),
            ("https::http://127.0.0.1/", "remote-helper syntax"),
            ("ssh::helper", "remote-helper syntax"),
            ("file::helper", "remote-helper syntax"),
        ),
    )
    @pytest.mark.parametrize("source_scheme", ("https", "http"))
    def test_clone_rejects_effective_insecure_transport_rewrite(
        self,
        tmp_path,
        replacement: str,
        message: str,
        source_scheme: str,
    ) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{replacement}.insteadOf",
            "GIT_CONFIG_VALUE_0": source_scheme,
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ),
            pytest.raises(GitUrlRewriteError, match=message),
        ):
            clone_git_worktree(
                f"{source_scheme}://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

    def test_clone_uses_longest_matching_url_rewrite(self, tmp_path) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "url.http://bad.example/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://git.example.com/",
            "GIT_CONFIG_KEY_1": "url.file:///fixture/.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://git.example.com/acme/",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        clone_args = run.call_args_list[-1].args[0]
        assert "clone" in clone_args
        assert "core.hooksPath=/dev/null" in clone_args

    def test_clone_ignores_invoking_repository_local_url_rewrite(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        invoking = tmp_path / "invoking"
        invoking.mkdir()
        _REAL_SUBPROCESS_RUN(
            ["git", "-C", str(invoking), "init"],
            check=True,
            capture_output=True,
        )
        _REAL_SUBPROCESS_RUN(
            [
                "git",
                "-C",
                str(invoking),
                "config",
                "url.http://example.com/.insteadOf",
                "https://git.example.com/",
            ],
            check=True,
            capture_output=True,
        )
        config = tmp_path / "global-gitconfig"
        config.write_text("", encoding="ascii")
        monkeypatch.chdir(invoking)

        with (
            patch.dict(
                os.environ,
                {
                    "PATH": os.environ["PATH"],
                    "GIT_CONFIG_GLOBAL": str(config),
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
                clear=True,
            ),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
            )

        clone_args = run.call_args_list[-1].args[0]
        assert "clone" in clone_args
        assert "core.hooksPath=/dev/null" in clone_args

    def test_clone_rejects_target_activated_include_rewrite(
        self,
        tmp_path,
    ) -> None:
        target = tmp_path / "clone"
        included = tmp_path / "target-gitconfig"
        included.write_text(
            '[url "http://mirror.example/"]\n\tinsteadOf = https://git.example.com/\n',
            encoding="ascii",
        )
        global_config = tmp_path / "global-gitconfig"
        global_config.write_text(
            f'[includeIf "gitdir:**/clone/.git"]\n\tpath = {included.as_posix()}\n',
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with (
            patch.dict(os.environ, env, clear=True),
            patch("apm_cli.utils.git_env.subprocess.run") as run,
            pytest.raises(GitUrlRewriteError, match="insecure HTTP"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                target,
                env=env,
            )

        run.assert_not_called()

    def test_clone_rejects_remote_activated_include_rewrite(self, tmp_path) -> None:
        target = tmp_path / "clone"
        included = tmp_path / "remote-gitconfig"
        included.write_text(
            '[url "http://mirror.example/"]\n\tinsteadOf = https://git.example.com/\n',
            encoding="ascii",
        )
        global_config = tmp_path / "global-gitconfig"
        global_config.write_text(
            '[includeIf "hasconfig:remote.*.url:https://git.example.com/**"]\n'
            f"\tpath = {included.as_posix()}\n",
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        with (
            patch.dict(os.environ, env, clear=True),
            patch("apm_cli.utils.git_env.subprocess.run") as run,
            pytest.raises(GitUrlRewriteError, match="insecure HTTP"),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                target,
                env=env,
            )

        run.assert_not_called()

    def test_network_env_freezes_global_rewrite_config(self, tmp_path) -> None:
        config = tmp_path / "gitconfig"
        config.write_text(
            '[url "file:///safe/"]\n\tinsteadOf = https://git.example.com/\n',
            encoding="ascii",
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        with patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True):
            child = git_network_env("https://git.example.com/acme/repo", env)

        config.write_text(
            '[url "http://unsafe.example/"]\n\tinsteadOf = https://git.example.com/\n',
            encoding="ascii",
        )
        result = _REAL_SUBPROCESS_RUN(
            [
                get_git_executable(),
                "config",
                "--null",
                "--get-regexp",
                r"^url\..*\.insteadOf$",
            ],
            capture_output=True,
            check=True,
            cwd=tmp_path,
            env=child,
        )

        replacements = []
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            key, _prefix = entry.decode("utf-8").split("\n", 1)
            replacements.append(urlsplit(key[4 : -len(".insteadof")]))
        assert [(item.scheme, item.hostname, item.path) for item in replacements] == [
            ("file", None, "/safe/")
        ]

    def test_remote_refs_replaces_ambient_repository_environment(self) -> None:
        config_result = subprocess.CompletedProcess(
            ["git", "config"],
            0,
            stdout=b"",
            stderr=b"",
        )
        network_result = subprocess.CompletedProcess(
            ["git", "ls-remote"],
            0,
            stdout="",
            stderr="",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "PATH": os.environ["PATH"],
                    "GIT_DIR": "/invoking/.git",
                    "GIT_WORK_TREE": "/invoking",
                },
                clear=True,
            ),
            patch("apm_cli.utils.git_env._git_config_run", return_value=config_result),
            patch("apm_cli.utils.git_env.subprocess.run", return_value=network_result) as run,
        ):
            git_remote_refs("https://git.example.com/acme/repo")

        child = run.call_args.kwargs["env"]
        assert "GIT_DIR" not in child
        assert "GIT_WORK_TREE" not in child
        assert run.call_args.kwargs["cwd"] == str(Path(get_git_executable()).resolve().parent)

    def test_remote_refs_timeout_does_not_expose_authenticated_url(self) -> None:
        token = "remote-ref-timeout-token"
        config_result = subprocess.CompletedProcess(
            ["git", "config"],
            0,
            stdout=b"",
            stderr=b"",
        )
        with (
            patch("apm_cli.utils.git_env._git_config_run", return_value=config_result),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["git", "ls-remote", f"https://{token}@git.example.com/repo"],
                    30,
                ),
            ),
            pytest.raises(subprocess.TimeoutExpired) as raised,
        ):
            git_remote_refs(f"https://{token}@git.example.com/repo")

        assert token not in str(raised.value)

    def test_remote_refs_redacts_authorization_trace_output(self) -> None:
        secret = "trace-secret"
        config_result = subprocess.CompletedProcess(
            ["git", "config"],
            0,
            stdout=b"",
            stderr=b"",
        )
        network_result = subprocess.CompletedProcess(
            ["git", "ls-remote"],
            1,
            stdout="",
            stderr=f"trace: Authorization: Basic {secret}\nfatal: denied",
        )
        with (
            patch("apm_cli.utils.git_env._git_config_run", return_value=config_result),
            patch("apm_cli.utils.git_env.subprocess.run", return_value=network_result),
        ):
            result = git_remote_refs("https://git.example.com/acme/repo")

        assert "Authorization: ******" in result.stderr
        assert secret not in result.stderr

    def test_clone_preserves_safe_existing_url_rewrite(self, tmp_path) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.file:///fixture/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://git.example.com/acme/repo",
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        child = run.call_args_list[-1].kwargs["env"]
        rewrite_entries = [
            (
                child[f"GIT_CONFIG_KEY_{index}"],
                child[f"GIT_CONFIG_VALUE_{index}"],
            )
            for index in range(int(child["GIT_CONFIG_COUNT"]))
            if child[f"GIT_CONFIG_KEY_{index}"].startswith("url.")
        ]
        assert len(rewrite_entries) == 1
        key, value = rewrite_entries[0]
        replacement = key[4 : -len(".insteadof")]
        assert urlsplit(replacement).scheme == "file"
        assert urlsplit(value).hostname == "git.example.com"

    def test_clone_materializes_parameter_rewrite_when_clearing_http_auth(
        self,
        tmp_path,
    ) -> None:
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_PARAMETERS": (
                "'url.file:///fixture/.insteadOf=https://git.example.com/' "
                "'http.extraheader=Authorization: Basic sentinel'"
            ),
        }
        with (
            patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=_run_real_git_config_and_fake_clone,
            ) as run,
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env=env,
            )

        child = run.call_args_list[-1].kwargs["env"]
        assert "GIT_CONFIG_PARAMETERS" not in child
        entries = {
            (
                child.get(f"GIT_CONFIG_KEY_{index}", ""),
                child.get(f"GIT_CONFIG_VALUE_{index}", ""),
            )
            for index in range(int(child["GIT_CONFIG_COUNT"]))
        }
        assert ("url.file:///fixture/.insteadof", "https://git.example.com/") in entries
        assert all(not value.lower().startswith("authorization:") for _, value in entries)

    def test_network_env_reuses_single_validated_rewrite_snapshot(self) -> None:
        remote_url = "https://git.example.com/acme/repo"
        rewrites = (("file:///fixture/", "https://git.example.com/"),)
        with (
            patch.dict(
                os.environ,
                {
                    "PATH": os.environ["PATH"],
                    "GIT_HTTP_EXTRAHEADER": "Authorization: Basic sentinel",
                },
                clear=True,
            ),
            patch(
                "apm_cli.utils.git_env._read_effective_git_config",
                return_value=_GitConfigSnapshot(
                    (
                        GitConfigEntry(
                            "command",
                            "url.file:///fixture/.insteadOf",
                            "https://git.example.com/",
                        ),
                        GitConfigEntry(
                            "command",
                            "http.extraheader",
                            "Authorization: Basic sentinel",
                        ),
                    ),
                    rewrites,
                    (
                        GitConfigEntry(
                            "command",
                            "http.extraheader",
                            "Authorization: Basic sentinel",
                        ),
                    ),
                ),
            ) as read_rewrites,
        ):
            child = git_network_env(remote_url)

        read_rewrites.assert_called_once()
        assert "GIT_HTTP_EXTRAHEADER" not in child
        assert child["GIT_CONFIG_KEY_0"] == "url.file:///fixture/.insteadOf"
        assert child["GIT_CONFIG_VALUE_0"] == "https://git.example.com/"

    def test_clone_streams_git_progress_to_reporter(self, tmp_path) -> None:
        reporter = MagicMock()
        handler = MagicMock()
        reporter.new_message_handler.return_value = handler
        process = MagicMock(returncode=0)

        def stream(_process, _stdout_handler, stderr_handler, **_kwargs) -> None:
            stderr_handler("Receiving objects: 50% (1/2)\n")

        with (
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch(
                "apm_cli.utils.git_env._validated_git_url_rewrite_policy",
                return_value=(None, _GitConfigSnapshot((), (), ())),
            ),
            patch(
                "apm_cli.utils.git_env._read_effective_git_config",
                return_value=_GitConfigSnapshot((), (), ()),
            ),
            patch(
                "apm_cli.utils.git_env._git_init_run",
                return_value=subprocess.CompletedProcess(["git", "init"], 0),
            ),
            patch("apm_cli.utils.git_env.subprocess.Popen", return_value=process) as popen,
            patch("git.cmd.handle_process_output", side_effect=stream),
        ):
            clone_git_worktree(
                "https://git.example.com/acme/repo",
                tmp_path / "clone",
                env={"PATH": os.environ.get("PATH", "")},
                progress=reporter,
            )

        handler.assert_called_once_with("Receiving objects: 50% (1/2)\n")
        assert "--progress" in popen.call_args.args[0]

    def test_clone_timeout_does_not_expose_authenticated_url(self, tmp_path) -> None:
        token = "secret-timeout-token"
        with (
            patch(
                "apm_cli.utils.git_env._validated_git_url_rewrite_policy",
                return_value=(None, _GitConfigSnapshot((), (), ())),
            ),
            patch(
                "apm_cli.utils.git_env.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["git", "clone", f"https://{token}@git.example.com/repo"],
                    300,
                ),
            ),
            pytest.raises(subprocess.TimeoutExpired) as raised,
        ):
            clone_git_worktree(
                f"https://{token}@git.example.com/repo",
                tmp_path / "clone",
                env={"PATH": os.environ.get("PATH", "")},
            )

        assert token not in str(raised.value)
