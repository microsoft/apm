"""End-to-end test for the parallel sparse-checkout race fix (#1126).

This test exercises the parallel download race directly against the
real GitHub-hosted ``github/awesome-copilot`` repo with two sibling
subdirectory dependencies sharing the same ``(host, owner, repo, ref)``
cache key.

Pre-#1126 fix, this test reliably failed with
``RuntimeError("Subdirectory '...' not found in repository")`` because
the v1 cache materialized one subdir at the cache layer and the second
consumer found the cached dir without its expected subdir.

Parametrized across ``ref_kind`` to cover all three materialization
paths:
- ``symbolic-https``: ref="main" (the original 6.2 baseline)
- ``sha-https``: ref pinned to a known commit (exercises
  ``_bare_clone_with_fallback``'s 3-tier SHA path)
- ``default-branch``: no ref (exercises the no-ref path)

Marked ``integration`` so it only runs in the integration suite (it
requires network and a GitHub token like the rest of
``tests/integration/test_apm_dependencies.py``).
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.shared_clone_cache import SharedCloneCache
from apm_cli.models.dependency.reference import DependencyReference

# Two sibling subdirs under the same upstream repo+ref. Both are
# present on github/awesome-copilot at the time of writing; if either
# is removed upstream, swap with another pair from
# `gh api repos/github/awesome-copilot/contents/skills --jq '.[].name'`.
SUBDIR_A = "skills/acquire-codebase-knowledge"
SUBDIR_B = "skills/agent-governance"

# A historical commit on github/awesome-copilot main that contains
# both subdirs. Resolved at fixture-setup time via the GitHub API; if
# resolution fails, the sha-https variant is skipped (rare network /
# upstream-API issue). The KNOWN_SHA constant below is a fallback for
# offline scenarios where the resolver cannot reach the API.
KNOWN_SHA: str | None = None


def _resolve_known_sha() -> str | None:
    """Resolve a real commit SHA on github/awesome-copilot for the sha-https variant.

    Passes ``GH_TOKEN`` to the ``gh`` subprocess so the test does not depend
    on the developer's ambient ``gh auth login`` state -- CI workers will have
    ``GITHUB_APM_PAT`` (or ``GITHUB_TOKEN``) but no ``gh`` config (Copilot
    review #1135).
    """
    import subprocess

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_APM_PAT")
    if not token:
        return None
    env = {**os.environ, "GH_TOKEN": token}

    try:
        result = subprocess.run(
            ["gh", "api", "repos/github/awesome-copilot/commits/main", "--jq", ".sha"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            sha = result.stdout.strip()
            if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                return sha
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return None


@pytest.mark.integration
@pytest.mark.parametrize(
    "ref_kind,ref_value",
    [
        ("symbolic-https", "main"),
        ("default-branch", None),
        ("sha-https", "RESOLVE_AT_RUNTIME"),
    ],
)
def test_two_subdirs_same_repo_parallel(ref_kind: str, ref_value: str | None) -> None:
    """Two sibling subdir deps from same repo+ref download in parallel.

    Asserts:
      1. Both subdir packages materialize with their expected content.
      2. No ``RuntimeError("Subdirectory ... not found")`` raised.
      3. Both consumers receive the same ``resolved_commit`` (cache hit
         on second consumer).
    """
    github_token = os.getenv("GITHUB_APM_PAT") or os.getenv("GITHUB_TOKEN")
    if not github_token:
        pytest.skip("GitHub token required (GITHUB_APM_PAT or GITHUB_TOKEN)")

    if ref_kind == "sha-https":
        ref_value = _resolve_known_sha()
        if ref_value is None:
            pytest.skip("Could not resolve a known SHA on github/awesome-copilot/main")

    test_dir = Path(tempfile.mkdtemp(prefix="apm_e2e_1126_"))
    try:
        # Build two dep refs sharing the same (host, owner, repo, ref)
        # cache key so they race through SharedCloneCache.
        ref_suffix = f"#{ref_value}" if ref_value else ""
        dep_a = DependencyReference.parse(f"github/awesome-copilot/{SUBDIR_A}{ref_suffix}")
        dep_b = DependencyReference.parse(f"github/awesome-copilot/{SUBDIR_B}{ref_suffix}")

        target_a = test_dir / "modules" / "a"
        target_b = test_dir / "modules" / "b"
        target_a.parent.mkdir(parents=True, exist_ok=True)

        # One downloader sharing the cache - mirrors install/phases/resolve.py
        # which attaches a single SharedCloneCache to the downloader.
        downloader = GitHubPackageDownloader()
        (test_dir / ".cache").mkdir()
        with SharedCloneCache(base_dir=test_dir / ".cache") as shared_cache:
            downloader.shared_clone_cache = shared_cache

            # Drive both downloads in parallel via ThreadPoolExecutor
            # (mirrors apm_resolver.py parallel BFS dispatch).
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                fa = ex.submit(downloader.download_subdirectory_package, dep_a, target_a)
                fb = ex.submit(downloader.download_subdirectory_package, dep_b, target_b)
                # Both must succeed without the v1
                # "Subdirectory ... not found" error.
                pkg_a = fa.result(timeout=120)
                pkg_b = fb.result(timeout=120)

        # Both subdirs must have materialized with content.
        assert target_a.exists(), f"{SUBDIR_A} not materialized"
        assert target_b.exists(), f"{SUBDIR_B} not materialized"
        assert any(target_a.iterdir()), f"{SUBDIR_A} is empty"
        assert any(target_b.iterdir()), f"{SUBDIR_B} is empty"

        # Lockfile parity (Copilot review #1135 + panel follow-up):
        # The canonical resolved SHA path is ``pkg.resolved_reference.resolved_commit``
        # (set in ``apm_package.py``). Asserting on it directly catches
        # silent regressions where the cache hit fails to propagate the
        # SHA back into the consumer's resolved reference.
        sha_a = pkg_a.resolved_reference.resolved_commit
        sha_b = pkg_b.resolved_reference.resolved_commit
        assert sha_a is not None and sha_a != "unknown" and len(sha_a) == 40, (
            f"expected resolved 40-char SHA for {SUBDIR_A}, got {sha_a!r}"
        )
        assert sha_b is not None and sha_b != "unknown" and len(sha_b) == 40, (
            f"expected resolved 40-char SHA for {SUBDIR_B}, got {sha_b!r}"
        )
        assert sha_a == sha_b, (
            f"Sibling subdirs from same repo+ref must resolve to "
            f"same commit, got a={sha_a} b={sha_b}"
        )
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)
