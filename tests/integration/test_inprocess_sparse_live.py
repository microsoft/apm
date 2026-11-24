"""Live: a monorepo subdirectory dependency fetched in process installs exactly what git installs.

Real public github.com monorepos at pinned commits: ``microsoft/apm`` (root
``.gitattributes`` with linguist-only rules) and ``github/awesome-copilot`` (root
``* text=auto eol=lf`` plus ``*.bat``/``*.cmd`` ``eol=crlf`` that no installed cone
matches). Installed once with the sparse in-process fetch and once with git forced
(APM_NO_INPROCESS_SPARSE=1): identical lockfiles, deployed bytes and deploy-store
receipts. On Linux and macOS every directory dependency is fetched in process.

Requires network access. Run: uv run pytest tests/integration/test_inprocess_sparse_live.py -m live
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.utils.git_env import reset_git_cache
from tests.fixtures.perf.generator import consumer_manifest
from tests.fixtures.perf.real_network import AWESOME_COPILOT_SHA
from tests.integration.test_inprocess_fetch_world import _receipts, _tree

pytestmark = [
    pytest.mark.live,
    pytest.mark.requires_network_integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

_AC = "github/awesome-copilot"
_SUBDIR_DEPS = (
    "microsoft/apm/packages/apm-guide#v0.37.0",
    f"{_AC}/skills/review-and-refactor#{AWESOME_COPILOT_SHA}",
    f"{_AC}/agents/janitor.agent.md#{AWESOME_COPILOT_SHA}",
    f"{_AC}/instructions/go.instructions.md#{AWESOME_COPILOT_SHA}",
)
_ENGAGED = "In-process sparse fetch: "


@pytest.fixture(autouse=True)
def _fresh_ambient_git_config():
    reset_git_cache()
    yield
    reset_git_cache()


def _install(apm: Path, project: Path, cache: Path, extra: dict[str, str]) -> int:
    env = {
        **os.environ,
        **extra,
        "APM_LOG_LEVEL": "DEBUG",
        "APM_CACHE_DIR": str(cache),
        "APM_NON_INTERACTIVE": "1",
    }
    result = subprocess.run(
        [str(apm), "install"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return result.stderr.count(_ENGAGED)


def test_sparse_in_process_and_git_install_identically(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "apm.yml").write_text(
        consumer_manifest("sparse-live", list(_SUBDIR_DEPS), ("copilot",)), encoding="utf-8"
    )
    fast, slow = tmp_path / "in-process", tmp_path / "git"
    shutil.copytree(template, fast)
    shutil.copytree(template, slow)
    engaged = _install(apm_binary_path, fast, tmp_path / "cache-fast", {})
    forced = _install(
        apm_binary_path, slow, tmp_path / "cache-git", {"APM_NO_INPROCESS_SPARSE": "1"}
    )

    assert forced == 0
    if sys.platform != "win32":
        # Windows engages only with Git for Windows' OpenSSL backend and a named CA.
        # Single-file dependencies take the file download path, not a sparse checkout.
        assert engaged == sum(1 for dep in _SUBDIR_DEPS if not dep.split("#")[0].endswith(".md"))
    assert (fast / "apm.lock.yaml").read_bytes() == (slow / "apm.lock.yaml").read_bytes()
    assert _tree(fast) == _tree(slow)
    fast_receipts = _receipts(tmp_path / "cache-fast")
    assert fast_receipts and fast_receipts == _receipts(tmp_path / "cache-git")
