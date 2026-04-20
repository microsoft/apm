"""Shared fixtures for the marketplace integration test suite.

Conventions
-----------
* All fixtures use tmp_path for filesystem isolation.
* git ls-remote is never called; RefResolver.list_remote_refs is patched
  via mock_ref_resolver where network access would otherwise be needed.
* The live_marketplace_repo fixture skips tests when APM_E2E_MARKETPLACE
  is not set.  It is used exclusively in test_live_e2e.py.
* run_cli invokes the real apm binary via subprocess so that CWD, env
  isolation, and exit codes are all captured faithfully.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.ref_resolver import RemoteRef


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path to the project root (two parents up from tests/integration/marketplace)
_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

# Path to the golden fixture
_GOLDEN_PATH = (
    _PROJECT_ROOT / "tests" / "fixtures" / "marketplace" / "golden.json"
)

# Environment variable that gates the live e2e tests
_LIVE_ENV_VAR = "APM_E2E_MARKETPLACE"


# ---------------------------------------------------------------------------
# Minimal valid marketplace.yml content
# ---------------------------------------------------------------------------

MINIMAL_YML = """\
name: test-marketplace
description: Test marketplace for integration tests
version: 1.0.0
owner:
  name: Test Org
  email: test@example.com
  url: https://example.com
metadata:
  pluginRoot: plugins
  category: testing
packages:
  - name: code-reviewer
    description: Automated code review assistant
    source: acme/code-reviewer
    version: "^2.0.0"
    tags:
      - review
      - quality
  - name: test-generator
    description: Test generation tool
    source: acme/test-generator
    version: "^1.0.0"
    subdir: src/plugin
    tags:
      - testing
"""

# marketplace.yml that matches the golden.json fixture exactly
# (SHAs are injected by mock_ref_resolver_golden)
GOLDEN_YML = """\
name: acme-tools
description: Curated developer tools by Acme Corp
version: 1.0.0
owner:
  name: Acme Corp
  email: tools@acme.example.com
  url: https://acme.example.com
metadata:
  pluginRoot: plugins
  category: developer-tools
packages:
  - name: code-reviewer
    description: Automated code review assistant
    source: acme/code-reviewer
    version: "^2.0.0"
    tags:
      - review
      - quality
  - name: test-generator
    source: acme/test-generator
    version: "^1.0.0"
    subdir: src/plugin
    tags:
      - testing
"""


# ---------------------------------------------------------------------------
# Core filesystem fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mkt_repo_root(tmp_path: Path) -> Path:
    """Return a tmp directory containing a valid marketplace.yml.

    The directory is suitable as the CWD for ``run_cli`` calls and for
    constructing a ``MarketplaceBuilder`` directly.
    """
    yml_path = tmp_path / "marketplace.yml"
    yml_path.write_text(MINIMAL_YML, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def golden_marketplace_json() -> dict:
    """Load tests/fixtures/marketplace/golden.json and return as a dict."""
    if not _GOLDEN_PATH.exists():
        pytest.skip(
            f"Golden fixture not found at {_GOLDEN_PATH}. "
            "Ensure tests/fixtures/marketplace/golden.json is present."
        )
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# RemoteRef factories for common test scenarios
# ---------------------------------------------------------------------------


def _make_refs_for_code_reviewer() -> List[RemoteRef]:
    """Refs for acme/code-reviewer: tags v2.0.0, v2.1.0, v3.0.0."""
    return [
        RemoteRef(
            name="refs/tags/v2.0.0",
            sha="aaaa000000000000000000000000000000000001",
        ),
        RemoteRef(
            name="refs/tags/v2.1.0",
            sha="abcd234567890abcdef1234567890abcdef12345",
        ),
        RemoteRef(
            name="refs/tags/v3.0.0",
            sha="bbbb000000000000000000000000000000000002",
        ),
        RemoteRef(name="refs/heads/main", sha="cccc000000000000000000000000000000000003"),
    ]


def _make_refs_for_test_generator() -> List[RemoteRef]:
    """Refs for acme/test-generator: tags v1.0.0, v1.0.3."""
    return [
        RemoteRef(
            name="refs/tags/v1.0.0",
            sha="1111000000000000000000000000000000000001",
        ),
        RemoteRef(
            name="refs/tags/v1.0.3",
            sha="def4567890abcdef1234567890abcdef12345678",
        ),
        RemoteRef(name="refs/heads/main", sha="dddd000000000000000000000000000000000004"),
    ]


def _ref_side_effect(owner_repo: str) -> List[RemoteRef]:
    """Return appropriate refs based on owner/repo slug."""
    mapping = {
        "acme/code-reviewer": _make_refs_for_code_reviewer(),
        "acme/test-generator": _make_refs_for_test_generator(),
    }
    if owner_repo in mapping:
        return mapping[owner_repo]
    # For unknown repos, return an empty list so tests fail deterministically
    return []


# ---------------------------------------------------------------------------
# mock_ref_resolver fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_ref_resolver():
    """Patch RefResolver.list_remote_refs with preset RemoteRef responses.

    Returns the MagicMock so tests can inspect call counts or override
    the side_effect for specific scenarios.

    Usage in tests::

        def test_something(mkt_repo_root, mock_ref_resolver):
            # mock_ref_resolver.list_remote_refs is already patched
            result = run_cli(["marketplace", "build"], cwd=mkt_repo_root)
            assert result.returncode == 0
    """
    with patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        side_effect=_ref_side_effect,
    ) as mock_obj:
        yield mock_obj


@pytest.fixture()
def mock_ref_resolver_golden():
    """Patch RefResolver so code-reviewer resolves to v2.1.0 and
    test-generator to v1.0.3 -- the exact SHAs in the golden fixture."""

    def _golden_side_effect(owner_repo: str) -> List[RemoteRef]:
        if owner_repo == "acme/code-reviewer":
            return [
                RemoteRef(
                    name="refs/tags/v2.1.0",
                    sha="abcd234567890abcdef1234567890abcdef12345",
                ),
            ]
        if owner_repo == "acme/test-generator":
            return [
                RemoteRef(
                    name="refs/tags/v1.0.3",
                    sha="def4567890abcdef1234567890abcdef12345678",
                ),
            ]
        return []

    with patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        side_effect=_golden_side_effect,
    ) as mock_obj:
        yield mock_obj


# ---------------------------------------------------------------------------
# Live e2e fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_marketplace_repo() -> str:
    """Return the value of APM_E2E_MARKETPLACE or skip the test.

    The value is validated to match the ``owner/repo`` format before
    being returned.  When the env var is absent the test is skipped with
    a clear message so CI never fails due to a missing variable.

    Returns
    -------
    str
        The ``owner/repo`` value from APM_E2E_MARKETPLACE.

    Raises
    ------
    pytest.skip.Exception
        When APM_E2E_MARKETPLACE is not set.
    """
    value = os.environ.get(_LIVE_ENV_VAR, "").strip()
    if not value:
        pytest.skip(
            f"{_LIVE_ENV_VAR} is not set. "
            "Set it to an owner/repo string (e.g. my-org/my-marketplace) "
            "to run the live e2e tests locally."
        )

    parts = value.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        pytest.skip(
            f"{_LIVE_ENV_VAR}={value!r} is not in 'owner/repo' format. "
            "Correct it and re-run."
        )

    return value


# ---------------------------------------------------------------------------
# run_cli helper
# ---------------------------------------------------------------------------


def run_cli(
    args: List[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Invoke ``uv run apm <args>`` via subprocess.

    The subprocess inherits a curated env containing PATH and HOME.
    Additional env vars can be passed via *env*; they are merged on top.

    Parameters
    ----------
    args:
        CLI arguments after ``apm`` (e.g. ``["marketplace", "build"]``).
    cwd:
        Working directory for the subprocess.  Defaults to the project root.
    env:
        Extra environment variables to add (or override) in the subprocess.
    timeout:
        Maximum seconds to wait before raising TimeoutExpired.

    Returns
    -------
    subprocess.CompletedProcess
        With stdout, stderr as str (text=True).
    """
    base_env: dict = {}

    # Propagate essential host env vars
    for key in ("PATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
        if key in os.environ:
            base_env[key] = os.environ[key]

    # Propagate the live marketplace env var if set (so live tests work)
    if _LIVE_ENV_VAR in os.environ:
        base_env[_LIVE_ENV_VAR] = os.environ[_LIVE_ENV_VAR]

    # Propagate GITHUB_TOKEN / GH_TOKEN when present
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if key in os.environ:
            base_env[key] = os.environ[key]

    # Caller overrides applied last
    if env:
        base_env.update(env)

    cmd = [sys.executable, "-m", "uv", "run", "apm"] + args
    # Prefer the project-local uv wrapper if available
    uv_bin = _project_uv_bin()
    if uv_bin:
        cmd = [uv_bin, "run", "apm"] + args

    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=base_env,
    )


def _project_uv_bin() -> Optional[str]:
    """Return path to uv if it is on PATH, else None."""
    import shutil

    return shutil.which("uv")
