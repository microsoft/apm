"""Unit tests for src/apm_cli/deps/transport_selection.py.

Covers the selection matrix from issue microsoft/apm#778:

* explicit ssh:// URLs are strict (no HTTPS fallback) -- closes #661
* explicit https:// URLs are strict (no SSH fallback)
* explicit http:// URLs are strict and never use token auth
* shorthand consults git insteadOf rewrites and prefers SSH on rewrite hits
  -- closes #328
* shorthand defaults to HTTPS-only when no rewrite, no CLI pref, no env
* CLI / env overrides for shorthand: --ssh / --https, APM_GIT_PROTOCOL
* APM_ALLOW_PROTOCOL_FALLBACK / --allow-protocol-fallback restores the
  legacy permissive cross-protocol chain so today's clones still succeed
* token presence drives whether auth-HTTPS is in the plan
* per-instance cache: insteadOf rewrites are loaded once
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional  # noqa: F401, UP035
from unittest.mock import patch

import pytest

from apm_cli.deps.transport_selection import (
    ENV_ALLOW_FALLBACK,
    ENV_PROTOCOL,
    FALLBACK_HINT,
    GitConfigInsteadOfResolver,
    InsteadOfResolver,  # noqa: F401
    NoOpInsteadOfResolver,  # noqa: F401
    ProtocolPreference,
    TransportAttempt,  # noqa: F401
    TransportPlan,
    TransportSelector,
    is_fallback_allowed,
    protocol_pref_from_env,
)
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeInsteadOfResolver:
    """Test double for ``InsteadOfResolver``.

    Constructed with a dict ``{candidate_prefix: replacement_prefix}``; the
    ``resolve`` call returns the rewritten URL when a prefix matches, mirroring
    git's ``url.<base>.insteadof`` semantics.
    """

    def __init__(self, rewrites: dict[str, str] | None = None):
        self._rewrites = rewrites or {}
        self.calls: list[str] = []

    def resolve(self, candidate_url: str) -> str | None:
        self.calls.append(candidate_url)
        for prefix, replacement in self._rewrites.items():
            if candidate_url.startswith(prefix):
                return replacement + candidate_url[len(prefix) :]
        return None

    def has_exact_rule(self, candidate_url: str) -> bool:
        return candidate_url in self._rewrites


def _dep(spec: str) -> DependencyReference:
    return DependencyReference.parse(spec)


def _scheme_labels(plan: TransportPlan) -> list[str]:
    return [a.scheme for a in plan.attempts]


# ---------------------------------------------------------------------------
# Selection matrix
# ---------------------------------------------------------------------------


class TestExplicitSchemeStrict:
    """Explicit ssh:// / https:// URLs must not silently change protocol."""

    def test_explicit_ssh_strict_single_attempt(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("ssh://git@github.com/owner/repo.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=True,
        )
        assert plan.strict is True
        assert _scheme_labels(plan) == ["ssh"]
        assert plan.fallback_hint == FALLBACK_HINT

    def test_explicit_https_with_token_uses_auth_https(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("https://github.com/owner/repo.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=True,
        )
        assert plan.strict is True
        assert _scheme_labels(plan) == ["https"]
        assert plan.attempts[0].use_token is True

    def test_explicit_https_without_token_uses_plain_https(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("https://gitlab.com/acme/lib.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=False,
        )
        assert plan.strict is True
        assert _scheme_labels(plan) == ["https"]
        assert plan.attempts[0].use_token is False

    def test_explicit_https_reports_effective_file_rewrite(self):
        candidate = "https://github.com/owner/repo.git"
        selector = TransportSelector(
            insteadof_resolver=FakeInsteadOfResolver({"https://github.com/": "file:///mirror/"})
        )

        plan = selector.select(
            dep_ref=_dep(candidate),
            has_token=True,
            candidate_url=candidate,
        )

        assert _scheme_labels(plan) == ["file"]
        assert plan.attempts[0].use_token is False
        assert plan.attempts[0].requested_url == candidate
        assert plan.attempts[0].effective_url == "file:///mirror/owner/repo.git"

    def test_rewrite_does_not_duplicate_dot_git_suffix(self):
        resolver = FakeInsteadOfResolver(
            {"https://github.com/owner/repo": "file:///tmp/mirror/repo.git"}
        )

        plan = TransportSelector(insteadof_resolver=resolver).select(
            dep_ref=_dep("https://github.com/owner/repo.git"),
            candidate_url="https://github.com/owner/repo.git",
        )

        assert plan.attempts[0].requested_url == "https://github.com/owner/repo"
        assert plan.attempts[0].effective_url == "file:///tmp/mirror/repo.git"
        assert resolver.calls == [
            "https://github.com/owner/repo.git",
            "https://github.com/owner/repo",
        ]

    def test_exact_dot_git_rule_is_never_substituted(self):
        candidate = "https://github.com/owner/repo.git"
        resolver = FakeInsteadOfResolver({candidate: "file:///tmp/exact/repo.git.git"})

        plan = TransportSelector(insteadof_resolver=resolver).select(
            dep_ref=_dep(candidate),
            candidate_url=candidate,
        )

        assert plan.attempts[0].requested_url == candidate
        assert plan.attempts[0].effective_url == "file:///tmp/exact/repo.git.git"
        assert resolver.calls == [candidate]

    def test_explicit_http_is_strict_and_never_uses_token(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("http://gitlab.company.internal/acme/lib.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=True,
        )
        assert plan.strict is True
        assert _scheme_labels(plan) == ["http"]
        assert plan.attempts[0].use_token is False

    def test_explicit_ssh_ignores_cli_pref_https(self):
        """User-stated scheme on the dep wins over CLI default for shorthand."""
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("ssh://git@github.com/owner/repo.git"),
            cli_pref=ProtocolPreference.HTTPS,
            allow_fallback=False,
            has_token=True,
        )
        assert _scheme_labels(plan) == ["ssh"]
        assert plan.strict is True


class TestShorthandWithInsteadOf:
    """`git config url.<base>.insteadof` must be honored for shorthand deps."""

    def test_insteadof_https_to_ssh_prefers_ssh(self):
        rewrites = {"https://github.com/": "git@github.com:"}
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver(rewrites))
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=True,
        )
        assert _scheme_labels(plan) == ["ssh"]
        assert plan.attempts[0].requested_url == "https://github.com/owner/repo"
        assert plan.attempts[0].effective_url == "git@github.com:owner/repo"
        assert plan.attempts[0].use_token is False
        assert plan.strict is True

    def test_web_fallback_preserves_exact_dot_git_rewrite(self):
        resolver = FakeInsteadOfResolver(
            {"https://github.com/owner/repo.git": "file:///tmp/mirror/repo.git"}
        )

        plan = TransportSelector(insteadof_resolver=resolver).select(
            dep_ref=_dep("owner/repo"),
            candidate_url="owner/repo",
            allow_fallback=True,
            has_token=True,
        )

        assert plan.attempts[-1].requested_url == "https://github.com/owner/repo.git"
        assert plan.attempts[-1].effective_url == "file:///tmp/mirror/repo.git"

    def test_canonical_candidate_preserves_custom_port_and_suffix(self):
        candidate = "https://git.example.test:8443/acme/repo.git"
        resolver = FakeInsteadOfResolver(
            {"https://git.example.test:8443/": "ssh://git@git.example.test:8443/"}
        )
        plan = TransportSelector(insteadof_resolver=resolver).select(
            dep_ref=_dep("git.example.test:8443/acme/repo"),
            candidate_url=candidate,
        )

        assert resolver.calls == [candidate]
        assert plan.attempts[0].requested_url == candidate
        assert plan.attempts[0].effective_url == "ssh://git@git.example.test:8443/acme/repo.git"

    def test_https_preference_reports_effective_ssh_rewrite(self):
        candidate = "https://github.com/owner/repo"
        plan = TransportSelector(
            insteadof_resolver=FakeInsteadOfResolver({"https://github.com/": "git@github.com:"})
        ).select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.HTTPS,
            has_token=True,
            candidate_url=candidate,
        )

        assert _scheme_labels(plan) == ["ssh"]
        assert plan.attempts[0].use_token is False
        assert plan.attempts[0].requested_url == candidate

    def test_rewrite_deduplicates_equivalent_web_fallbacks(self):
        candidate = "https://github.com/owner/repo"
        plan = TransportSelector(
            insteadof_resolver=FakeInsteadOfResolver({"https://github.com/": "git@github.com:"})
        ).select(
            dep_ref=_dep("owner/repo"),
            allow_fallback=True,
            has_token=True,
            candidate_url=candidate,
        )

        assert _scheme_labels(plan) == ["ssh"]
        assert plan.strict is True
        assert "git config --show-origin" in (plan.fallback_hint or "")

    def test_no_insteadof_defaults_to_https_strict(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=True,
        )
        assert _scheme_labels(plan) == ["https"]
        assert plan.attempts[0].use_token is True
        assert plan.strict is True

    def test_no_insteadof_no_token_defaults_to_plain_https(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=False,
            has_token=False,
        )
        assert _scheme_labels(plan) == ["https"]
        assert plan.attempts[0].use_token is False
        assert plan.strict is True


class TestCliPreferences:
    """--ssh / --https flags steer shorthand transport selection."""

    def test_cli_pref_ssh_for_shorthand(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.SSH,
            allow_fallback=False,
            has_token=True,
        )
        assert plan.attempts[0].scheme == "ssh"

    def test_cli_pref_https_for_shorthand(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.HTTPS,
            allow_fallback=False,
            has_token=True,
        )
        assert plan.attempts[0].scheme == "https"
        assert plan.attempts[0].use_token is True

    def test_cli_pref_does_not_override_explicit_scheme(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("https://github.com/owner/repo.git"),
            cli_pref=ProtocolPreference.SSH,
            allow_fallback=False,
            has_token=True,
        )
        assert _scheme_labels(plan) == ["https"]


class TestAllowFallback:
    """allow_fallback=True restores the legacy permissive cross-protocol chain."""

    def test_shorthand_with_token_legacy_chain(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=True,
            has_token=True,
        )
        # legacy default for shorthand: auth-HTTPS, then SSH, then plain-HTTPS
        schemes = _scheme_labels(plan)
        assert "https" in schemes and "ssh" in schemes
        assert plan.strict is False

    def test_shorthand_without_token_legacy_chain(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("owner/repo"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=True,
            has_token=False,
        )
        schemes = _scheme_labels(plan)
        assert "ssh" in schemes and "https" in schemes
        assert plan.strict is False

    def test_explicit_https_with_allow_fallback_keeps_https_first(self):
        """Explicit https:// keeps HTTPS first when fallback is enabled."""
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("https://gitlab.com/acme/lib.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=True,
            has_token=True,
        )
        assert [(a.scheme, a.use_token) for a in plan.attempts] == [
            ("https", True),
            ("ssh", False),
            ("https", False),
        ]
        assert plan.strict is False

    def test_explicit_ssh_with_allow_fallback_keeps_ssh_first(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("ssh://git@github.com/owner/repo.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=True,
            has_token=True,
        )
        assert [(a.scheme, a.use_token) for a in plan.attempts] == [
            ("ssh", False),
            ("https", True),
            ("https", False),
        ]
        assert plan.strict is False

    def test_explicit_http_with_allow_fallback_keeps_http_first(self):
        sel = TransportSelector(insteadof_resolver=FakeInsteadOfResolver())
        plan = sel.select(
            dep_ref=_dep("http://gitlab.company.internal/acme/lib.git"),
            cli_pref=ProtocolPreference.NONE,
            allow_fallback=True,
            has_token=True,
        )
        assert [(a.scheme, a.use_token) for a in plan.attempts] == [
            ("http", False),
            ("ssh", False),
        ]
        assert plan.strict is False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestProtocolPrefFromEnv:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("ssh", ProtocolPreference.SSH),
            ("SSH", ProtocolPreference.SSH),
            ("https", ProtocolPreference.HTTPS),
            ("HTTPS", ProtocolPreference.HTTPS),
            ("", ProtocolPreference.NONE),
            ("auto", ProtocolPreference.NONE),
            ("garbage", ProtocolPreference.NONE),
        ],
    )
    def test_env_value(self, value, expected, monkeypatch):
        if value:
            monkeypatch.setenv(ENV_PROTOCOL, value)
        else:
            monkeypatch.delenv(ENV_PROTOCOL, raising=False)
        assert protocol_pref_from_env() is expected


class TestIsFallbackAllowed:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("", False),
        ],
    )
    def test_env_value(self, value, expected, monkeypatch):
        if value:
            monkeypatch.setenv(ENV_ALLOW_FALLBACK, value)
        else:
            monkeypatch.delenv(ENV_ALLOW_FALLBACK, raising=False)
        assert is_fallback_allowed() is expected


# ---------------------------------------------------------------------------
# Resolver caching
# ---------------------------------------------------------------------------


class TestGitConfigInsteadOfResolver:
    def test_lookup_cached_per_instance(self):
        """`git config --get-regexp` is shelled out at most once per instance."""
        resolver = GitConfigInsteadOfResolver()
        with patch("apm_cli.utils.git_env._git_config_run") as run:
            # Simulate one rewrite rule: https://github.com/ -> git@github.com:
            run.return_value.returncode = 0
            run.return_value.stdout = (
                b"command\0url.git@github.com:.insteadof\nhttps://github.com/\0"
            )
            resolver.resolve("https://github.com/owner/repo")
            resolver.resolve("https://github.com/other/proj")
            resolver.resolve("https://gitlab.com/acme/lib")
            assert run.call_count == 1

    def test_uses_sanitized_normal_env(self):
        """The resolver keeps user config while removing repository state.

        The downloader's locked-down git_env sets GIT_CONFIG_GLOBAL=/dev/null,
        which would suppress user insteadOf rewrites. Issue #328 stays broken
        unless the resolver starts from the normal environment.
        """
        resolver = GitConfigInsteadOfResolver()
        with (
            patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/home/test/.gitconfig",
                    "GIT_DIR": "/hook/repo.git",
                },
            ),
            patch("apm_cli.utils.git_env._git_config_run") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            resolver.resolve("https://github.com/owner/repo")
            _args, kwargs = run.call_args
            assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/home/test/.gitconfig"
            assert "GIT_DIR" not in kwargs["env"]

    def test_resolve_returns_none_when_no_rewrites(self):
        resolver = GitConfigInsteadOfResolver()
        with patch("apm_cli.utils.git_env._git_config_run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            assert resolver.resolve("https://github.com/owner/repo") is None
