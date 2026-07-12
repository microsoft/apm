"""Hermetic integration coverage for ADO ref-resolution credential retry."""

from types import SimpleNamespace
from unittest.mock import patch

from apm_cli.core.auth import AuthResolver
from apm_cli.install.phases.resolve import _maybe_resolve_git_semver
from apm_cli.models.dependency.reference import DependencyReference


def test_ado_semver_ref_resolution_retries_stale_pat_with_cli_bearer(
    monkeypatch,
) -> None:
    """Tag resolution succeeds when ADO rejects PAT and accepts az bearer."""
    monkeypatch.setenv("ADO_APM_PAT", "stale-test-pat")
    provider = SimpleNamespace(
        is_available=lambda: True,
        get_bearer_token=lambda: "fresh-test-bearer",
    )
    attempts = []

    def _run(args, **kwargs):
        auth_values = {
            value for key, value in kwargs["env"].items() if key.startswith("GIT_CONFIG_VALUE_")
        }
        scheme = "bearer" if any("Bearer " in value for value in auth_values) else "basic"
        attempts.append(scheme)
        if scheme == "bearer":
            return SimpleNamespace(
                returncode=0,
                stdout=f"{'b' * 40}\trefs/tags/v2.0.0\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: The requested URL returned error: 401",
        )

    dep = DependencyReference(
        host="dev.azure.com",
        repo_url="example/project/_git/package",
        reference="^2.0.0",
        source="git",
        explicit_scheme="https",
    )
    with (
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=provider),
        patch("apm_cli.marketplace.ref_resolver.subprocess.run", side_effect=_run),
    ):
        resolution = _maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=AuthResolver(),
        )

    assert resolution.resolved_tag == "v2.0.0"
    assert attempts == ["basic", "bearer"]


def test_bearer_fallback_scrubs_inherited_authorization_header(
    monkeypatch,
) -> None:
    """Fallback keeps the scrubbed context and exactly one auth header."""
    sentinel = "Basic AAAA_INHERITED_SENTINEL_AAAA"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "http.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", f"Authorization: {sentinel}")
    monkeypatch.setenv("ADO_APM_PAT", "stale-test-pat")
    monkeypatch.setenv("SAFE_CONTEXT_SENTINEL", "preserved")

    provider = SimpleNamespace(
        is_available=lambda: True,
        get_bearer_token=lambda: "fresh-test-bearer",
    )

    fallback_envs: list[dict[str, str]] = []

    def _run(args, **kwargs):
        env = kwargs["env"]
        auth_values = [
            value
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_VALUE_") and "Authorization:" in value
        ]
        if any("Bearer " in value for value in auth_values):
            fallback_envs.append(dict(env))
            return SimpleNamespace(
                returncode=0,
                stdout=f"{'a' * 40}\trefs/tags/v3.0.0\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: The requested URL returned error: 401",
        )

    dep = DependencyReference(
        host="dev.azure.com",
        repo_url="example/project/_git/package",
        reference="^3.0.0",
        source="git",
        explicit_scheme="https",
    )
    with (
        patch("apm_cli.core.azure_cli.get_bearer_provider", return_value=provider),
        patch("apm_cli.marketplace.ref_resolver.subprocess.run", side_effect=_run),
    ):
        resolution = _maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=AuthResolver(),
        )

    assert resolution.resolved_tag == "v3.0.0"
    assert len(fallback_envs) == 1

    env = fallback_envs[0]
    auth_headers = [
        value
        for key, value in env.items()
        if key.startswith("GIT_CONFIG_VALUE_") and "Authorization:" in value
    ]
    assert not any(sentinel in header for header in auth_headers)
    assert len(auth_headers) == 1
    assert env["SAFE_CONTEXT_SENTINEL"] == "preserved"
