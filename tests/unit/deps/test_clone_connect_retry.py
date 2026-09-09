"""Bounded HTTPS connection recovery below auth and protocol selection."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from git.exc import GitCommandError

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.bare_cache import bare_clone_with_fallback, clone_with_fallback
from apm_cli.deps.clone_engine import CloneEngine
from apm_cli.deps.transport_selection import ProtocolPreference, TransportAttempt, TransportPlan
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component

URL = "https://github.com/microsoft/apm-sample-package.git"
CONNECT = (
    f"fatal: unable to access '{URL}/': "
    "Failed to connect to github.com port 443 after 8126 ms: Couldn't connect to server"
)


@pytest.fixture
def host() -> MagicMock:
    """Provide one strict HTTPS attempt, without credential discovery or network."""
    context = MagicMock()
    context._protocol_pref = ProtocolPreference.HTTPS
    context._allow_fallback = False
    context._transport_selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", label="HTTPS", use_token=False)],
        strict=True,
    )
    context._resolve_dep_token.return_value = None
    context._resolve_dep_auth_ctx.return_value = None
    context.auth_resolver.uses_public_github_anonymous_first.return_value = False
    context.auth_resolver.build_error_context.return_value = ""
    context._build_repo_url.return_value = URL
    context._build_noninteractive_git_env.return_value = {"LC_ALL": "C"}
    context._sanitize_git_error.side_effect = lambda text: text
    context.has_ado_token = False
    return context


def _execute(host: MagicMock, target: Path, action: MagicMock) -> None:
    CloneEngine(host).execute(
        "microsoft/apm-sample-package",
        target,
        dep_ref=DependencyReference.parse("microsoft/apm-sample-package"),
        clone_action=action,
    )


@pytest.mark.parametrize(
    "stream", [CONNECT, CONNECT.encode(), f"Cloning into 'tmp'...\n{CONNECT}\n"]
)
@pytest.mark.parametrize("error_type", [subprocess.CalledProcessError, GitCommandError])
def test_connect_retry_preserves_action_arguments(
    host: MagicMock, tmp_path: Path, stream: str | bytes, error_type: type[Exception]
) -> None:
    """The observed diagnostic earns exactly one retry on the same URL/env/target."""
    error = (
        GitCommandError(["git", "clone"], 128, stderr=stream)
        if error_type is GitCommandError
        else subprocess.CalledProcessError(128, ["git", "clone"], stderr=stream)
    )
    action = MagicMock(side_effect=[error, None])
    with patch("apm_cli.deps.clone_engine.time.sleep") as sleep:
        _execute(host, tmp_path, action)
    assert action.call_count == 2
    first, second = action.call_args_list
    assert first == second
    assert first.args[0] == URL
    assert first.args[1] is second.args[1]
    sleep.assert_called_once_with(1)
    host._resolve_dep_token.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: Authentication failed"),
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: HTTP 403"),
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: SSL certificate problem"),
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: repository not found"),
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: invalid reference"),
        subprocess.CalledProcessError(128, ["git"], stderr=f"remote: {CONNECT}"),
        subprocess.CalledProcessError(
            128, ["git"], stderr=f"{CONNECT}\nfatal: Authentication failed"
        ),
        subprocess.CalledProcessError(128, ["git"], stderr=CONNECT.replace("8126", "invalid")),
        subprocess.CalledProcessError(
            128, ["git"], stderr=CONNECT.replace("github.com", "other.test")
        ),
        subprocess.CalledProcessError(128, ["git"], output=CONNECT),
        subprocess.CalledProcessError(1, ["git"], stderr=CONNECT),
        subprocess.TimeoutExpired(["git"], 10, stderr=CONNECT),
        ValueError("policy rejected"),
        AssertionError("invalid downloaded content"),
    ],
)
def test_non_connect_failure_is_not_retried(
    host: MagicMock, tmp_path: Path, error: Exception
) -> None:
    action = MagicMock(side_effect=error)
    with patch("apm_cli.deps.clone_engine.time.sleep") as sleep:
        with pytest.raises((RuntimeError, ValueError, AssertionError)):
            _execute(host, tmp_path, action)
    action.assert_called_once()
    sleep.assert_not_called()


def test_persistent_connection_failure_preserves_final_diagnostic(
    host: MagicMock, tmp_path: Path
) -> None:
    final = CONNECT.replace("8126", "9127")
    action = MagicMock(
        side_effect=[
            subprocess.CalledProcessError(128, ["git"], stderr=CONNECT),
            subprocess.CalledProcessError(128, ["git"], stderr=final),
        ]
    )
    with patch("apm_cli.deps.clone_engine.time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="after 9127 ms"):
            _execute(host, tmp_path, action)
    assert action.call_count == 2
    sleep.assert_called_once_with(1)


@pytest.mark.parametrize("persistent", [False, True])
def test_public_anonymous_retry_stays_inside_auth_callback(
    host: MagicMock, tmp_path: Path, persistent: bool
) -> None:
    host.auth_resolver.uses_public_github_anonymous_first.return_value = True
    env = {"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    resolver = AuthResolver()
    host.auth_resolver.try_with_fallback.side_effect = resolver.try_with_fallback
    error = subprocess.CalledProcessError(128, ["git"], stderr=CONNECT)
    action = MagicMock(side_effect=[error, error if persistent else None])
    with (
        patch("apm_cli.deps.clone_engine.time.sleep"),
        patch.object(resolver, "uses_public_github_anonymous_first", return_value=True),
        patch.object(resolver, "build_public_github_anonymous_git_env", return_value=env),
        patch.object(resolver, "resolve") as resolve,
        pytest.raises(RuntimeError) if persistent else nullcontext(),
    ):
        _execute(host, tmp_path, action)
    assert action.call_args_list == [call(URL, env, tmp_path), call(URL, env, tmp_path)]
    host.auth_resolver.try_with_fallback.assert_called_once()
    host._resolve_dep_token.assert_not_called()
    host._resolve_dep_auth_ctx.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.parametrize("persistent", [False, True])
def test_ado_retry_stays_inside_primary_auth_attempt(
    host: MagicMock, tmp_path: Path, persistent: bool
) -> None:
    url = "https://dev.azure.com/org/proj/_git/repo"
    host._build_repo_url.return_value = url
    host._resolve_dep_token.return_value = "test-pat"
    host._resolve_dep_auth_ctx.return_value = SimpleNamespace(auth_scheme="basic")
    host._transport_selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", label="HTTPS token", use_token=True)],
        strict=True,
    )
    host.auth_resolver.execute_with_bearer_fallback.side_effect = (
        AuthResolver().execute_with_bearer_fallback
    )
    stderr = (
        f"fatal: unable to access '{url}/': Failed to connect to dev.azure.com port 443 "
        "after 8126 ms: Couldn't connect to server"
    )
    error = subprocess.CalledProcessError(128, ["git"], stderr=stderr)
    action = MagicMock(side_effect=[error, error if persistent else None])
    with (
        patch("apm_cli.deps.clone_engine.time.sleep"),
        patch("apm_cli.core.azure_cli.get_bearer_provider") as bearer_provider,
        pytest.raises(RuntimeError) if persistent else nullcontext(),
    ):
        CloneEngine(host).execute(
            "org/proj/_git/repo",
            tmp_path,
            dep_ref=DependencyReference.parse("dev.azure.com/org/proj/_git/repo"),
            clone_action=action,
        )
    assert action.call_count == 2
    assert action.call_args_list[0] == action.call_args_list[1]
    host.auth_resolver.execute_with_bearer_fallback.assert_called_once()
    host.auth_resolver.build_ado_bearer_git_env.assert_not_called()
    bearer_provider.assert_not_called()


@pytest.mark.parametrize("bare", [False, True])
def test_clone_callbacks_clean_partial_target_before_retry(
    host: MagicMock, tmp_path: Path, bare: bool
) -> None:
    """Exercise real cleanup callbacks, not a mock which pretends cleanup happened."""
    target = tmp_path / "clone"
    attempts = 0

    def fail_then_succeed(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal attempts
        assert not target.exists()
        target.mkdir()
        (target / "partial").write_text("partial", encoding="ascii")
        attempts += 1
        # Bare clone already has a shallow-to-full fallback. Fail both tiers.
        if attempts <= (2 if bare else 1):
            raise subprocess.CalledProcessError(128, ["git", "clone"], stderr=CONNECT)
        return MagicMock()

    engine = CloneEngine(host)
    dep = DependencyReference.parse("microsoft/apm-sample-package")
    with patch("apm_cli.deps.clone_engine.time.sleep") as sleep:
        if bare:
            with (
                patch("apm_cli.deps.bare_cache.subprocess.run", side_effect=fail_then_succeed),
                patch(
                    "apm_cli.utils.git_env.git_clone_env",
                    side_effect=lambda _u, env, *_a, **_k: env,
                ),
                patch("apm_cli.deps.bare_cache._scrub_bare_remote_url"),
            ):
                bare_clone_with_fallback(
                    engine.execute, dep.repo_url, target, dep_ref=dep, ref=None, is_commit_sha=False
                )
        else:
            with (
                patch("apm_cli.utils.git_env.clone_git_worktree", side_effect=fail_then_succeed),
                patch("apm_cli.deps.bare_cache.Repo"),
            ):
                clone_with_fallback(engine.execute, dep.repo_url, target, dep_ref=dep)
    assert attempts == (3 if bare else 2)
    sleep.assert_called_once_with(1)


@pytest.mark.parametrize(
    "bypass",
    [
        "_clone(winning_url, git_env, target_path)",
        "_clone(attempt_url, attempt_env, target_path)",
        "_clone(url, _env_for(attempt, url), target_path)",
    ],
)
def test_registered_guard_rejects_retry_bypass(bypass: str) -> None:
    """Every transport/auth branch remains behind the single recovery owner."""
    from scripts.architecture_linter.runner import run_selected_rules

    root = Path(__file__).resolve().parents[3]
    owner = "src/apm_cli/deps/clone_engine.py"
    source = (root / owner).read_text(encoding="utf-8")
    rule = "transport-platform-clone-connect-retry"
    report = run_selected_rules(
        root,
        (rule,),
        source_overrides={
            owner: source.replace(bypass, bypass.replace("_clone(", "clone_action("))
        },
    )
    assert not report.failures
    assert any(item.rule_id == rule for item in report.violations)
