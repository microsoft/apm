"""Hermetic E2E test for ADO --update PAT->bearer fallback (#1212).

Reproduces the original bug:
    Stale ADO_APM_PAT + valid az login. ``apm install -g`` succeeds.
    ``apm install -g --update`` SHOULD also succeed (same env, same creds)
    but in v0.10.x failed with "Authentication failed for dev.azure.com"
    because _preflight_auth_check did not retry with the AAD bearer.

Strategy: hermetic. Inject a fake ``git`` and fake ``az`` onto PATH:
    - fake git rejects ls-remote against PAT URLs (rc=128 with 401 stderr)
      and accepts ls-remote when an http.extraHeader carrying a bearer
      JWT is set (via GIT_CONFIG_VALUE_0).
    - fake az returns a fixed JWT.

The test would have failed before this PR's pipeline.py rewrite because
preflight raised AuthenticationError on the first 401 without retrying.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only fake-binary E2E; Windows variant lives in test-integration.ps1",
)

FAKE_GIT = r"""#!/usr/bin/env python3
"""
"""Fake git for #1212 preflight bearer-fallback E2E.

Behavior:
    ls-remote: fail with 401 if no bearer header in env; succeed if
               GIT_CONFIG_VALUE_0 contains 'Bearer '.
    Anything else: rc=0 (no-op).
"""
FAKE_GIT += """
import os
import sys

argv = sys.argv[1:]

# Probe call from preflight: ls-remote --heads --exit-code <url>
if argv[:1] == ["ls-remote"]:
    # Look for a bearer header injected via GIT_CONFIG_VALUE_<n>.
    has_bearer = False
    for k, v in os.environ.items():
        if k.startswith("GIT_CONFIG_VALUE_") and "Bearer " in v:
            has_bearer = True
            break

    if has_bearer:
        # Bearer attempt: succeed.
        sys.stdout.write("abc123\\trefs/heads/main\\n")
        sys.exit(0)
    else:
        # PAT attempt: 401 reject. Stderr text must contain a signal in
        # _ADO_AUTH_FAILURE_SIGNALS so the predicate triggers fallback.
        sys.stderr.write(
            "fatal: unable to access 'https://dev.azure.com/...': "
            "The requested URL returned error: 401\\n"
        )
        sys.exit(128)

# Anything else (config, version): no-op success.
sys.exit(0)
"""

FAKE_AZ = r"""#!/usr/bin/env python3
"""
"""Fake az CLI for #1212 -- returns a fixed JWT.

Used by AzureCliBearerProvider.get_bearer_token. The JWT structure must
have three dot-separated base64url segments so ``str.startswith('eyJ')``
checks pass.
"""
FAKE_AZ += """
import json
import sys

argv = sys.argv[1:]

# az account get-access-token --resource <guid> --query accessToken -o tsv
if argv[:3] == ["account", "get-access-token", "--resource"]:
    # Decide output shape based on flags.
    if "-o" in argv and argv[argv.index("-o") + 1] == "tsv" and "--query" in argv:
        sys.stdout.write("eyJ" + "A" * 120 + ".payload" + "B" * 60 + ".signature\\n")
    else:
        sys.stdout.write(json.dumps({
            "accessToken": "eyJ" + "A" * 120 + ".payload" + "B" * 60 + ".signature",
            "expiresOn": "2099-01-01 00:00:00.000000",
            "subscription": "fake-sub",
            "tenant": "fake-tenant",
            "tokenType": "Bearer",
        }) + "\\n")
    sys.exit(0)

# az account show / az login etc: not used by APM.
sys.exit(0)
"""


def _write_fake(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_preflight_falls_back_from_stale_pat_to_bearer(tmp_path, monkeypatch):
    """The bug: stale PAT + valid az login -> --update should succeed.

    Drives _preflight_auth_check directly (not the whole `apm install`
    pipeline) so the test stays hermetic and fast. The pipeline-level
    integration is exercised by the existing test_ado_bearer_e2e.py
    suite when APM_TEST_ADO_BEARER=1.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake(bin_dir / "git", FAKE_GIT)
    _write_fake(bin_dir / "az", FAKE_AZ)

    # Reset env so only our fakes are visible.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setenv("ADO_APM_PAT", "stale-pat-value")
    # Drop any inherited bearer cache from the real azure CLI test env.
    monkeypatch.delenv("AZURE_CLI_TEST_DEV_SP_NAME", raising=False)

    # Clear the process-wide bearer provider cache; otherwise a token from
    # a previous test (or real az login) would short-circuit our fake.
    from apm_cli.core.azure_cli import get_bearer_provider

    get_bearer_provider().clear_cache()

    from unittest.mock import MagicMock

    from apm_cli.core.auth import AuthResolver
    from apm_cli.install.pipeline import _preflight_auth_check

    # Build a real-enough dep ref + ctx.
    dep = MagicMock()
    dep.host = "dev.azure.com"
    dep.repo_url = "myorg/myproject/_git/myrepo"
    dep.port = None
    dep.is_azure_devops.return_value = True
    dep.explicit_scheme = None
    dep.is_insecure = False
    dep.ado_organization = "myorg"
    dep.ado_project = "myproject"
    dep.ado_repo = "myrepo"

    ctx = MagicMock()
    ctx.deps_to_install = [dep]
    ctx.update_refs = True

    # Real AuthResolver -- this drives execute_with_bearer_fallback through
    # the real azure_cli provider, which shells out to our fake `az`.
    resolver = AuthResolver()

    # Should NOT raise. Before the fix, this raised AuthenticationError.
    _preflight_auth_check(ctx, resolver, verbose=False)


def test_preflight_still_raises_when_both_pat_and_bearer_fail(tmp_path, monkeypatch):
    """Negative path: bearer-also-fails must still raise AuthenticationError.

    Same scenario as above but the fake git rejects the bearer attempt too
    (we strip the bearer-header check). Confirms the fallback isn't a
    blanket success-painter -- a truly broken auth state still surfaces.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    fake_git_always_401 = """#!/usr/bin/env python3
import sys
if sys.argv[1:2] == ["ls-remote"]:
    sys.stderr.write(
        "fatal: Authentication failed for "
        "'https://dev.azure.com/myorg/myproject/_git/myrepo'\\n"
    )
    sys.exit(128)
sys.exit(0)
"""
    _write_fake(bin_dir / "git", fake_git_always_401)
    _write_fake(bin_dir / "az", FAKE_AZ)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setenv("ADO_APM_PAT", "stale-pat-value")
    monkeypatch.delenv("AZURE_CLI_TEST_DEV_SP_NAME", raising=False)

    from apm_cli.core.azure_cli import get_bearer_provider

    get_bearer_provider().clear_cache()

    from unittest.mock import MagicMock

    from apm_cli.core.auth import AuthResolver
    from apm_cli.install.errors import AuthenticationError
    from apm_cli.install.pipeline import _preflight_auth_check

    dep = MagicMock()
    dep.host = "dev.azure.com"
    dep.repo_url = "myorg/myproject/_git/myrepo"
    dep.port = None
    dep.is_azure_devops.return_value = True
    dep.explicit_scheme = None
    dep.is_insecure = False
    dep.ado_organization = "myorg"
    dep.ado_project = "myproject"
    dep.ado_repo = "myrepo"

    ctx = MagicMock()
    ctx.deps_to_install = [dep]
    ctx.update_refs = True

    resolver = AuthResolver()

    with pytest.raises(AuthenticationError) as exc_info:
        _preflight_auth_check(ctx, resolver, verbose=False)

    # bearer_also_failed signal should be in the diagnostic context.
    diag = exc_info.value.diagnostic_context or ""
    assert "bearer" in diag.lower() or "az cli" in diag.lower()
