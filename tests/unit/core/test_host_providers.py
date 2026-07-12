"""Credential-scope regressions for manifest host-type hints."""

from __future__ import annotations

import os
from unittest.mock import patch

from apm_cli.core.auth import AuthResolver


def test_gitlab_hint_does_not_route_global_pat_to_untrusted_host() -> None:
    sentinel = "gitlab-global-sentinel"
    with patch.dict(os.environ, {"GITLAB_APM_PAT": sentinel}, clear=True):
        context = AuthResolver(allow_external_fallback=False).resolve(
            "packages.attacker.example",
            host_type="gitlab",
        )

    assert context.host_info.kind == "gitlab"
    assert context.token is None
    assert context.source == "none"


def test_canonical_gitlab_host_still_uses_global_pat() -> None:
    sentinel = "gitlab-global-sentinel"
    with patch.dict(os.environ, {"GITLAB_APM_PAT": sentinel}, clear=True):
        context = AuthResolver(allow_external_fallback=False).resolve("gitlab.com")

    assert context.token == sentinel
    assert context.source == "GITLAB_APM_PAT"


def test_user_trusted_gitlab_host_uses_global_pat() -> None:
    sentinel = "gitlab-global-sentinel"
    env = {
        "APM_GITLAB_HOSTS": "gitlab.corp.example",
        "GITLAB_APM_PAT": sentinel,
    }
    with patch.dict(os.environ, env, clear=True):
        context = AuthResolver(allow_external_fallback=False).resolve(
            "gitlab.corp.example",
            host_type="gitlab",
        )

    assert context.token == sentinel
    assert context.source == "GITLAB_APM_PAT"
