"""Integration tests for bare_cache.fetch_sha_into_bare with real git."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apm_cli.deps.bare_cache import fetch_sha_into_bare
from apm_cli.models.apm_package import DependencyReference


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_execute(src: Path) -> Callable[..., None]:
    """Return an execute_transport_plan that runs the clone_action locally."""

    def execute_transport_plan(
        url: str,
        target: Path,
        *,
        dep_ref: DependencyReference,
        clone_action: Callable[..., None],
        **kwargs: Any,
    ) -> None:
        # Use the local src path as the URL so no network access is needed
        clone_action(url=str(src), env={}, target=target)

    return execute_transport_plan


class TestFetchShaIntoBareIntegration:
    """Real-git integration tests for fetch_sha_into_bare."""

    def test_fetch_pins_ref_in_real_bare_repo(self, tmp_path: Path) -> None:
        """fetch_sha_into_bare creates refs/heads/apm-pin-<sha-prefix> in a real bare."""
        # 1. Create a source repo with 2 commits
        src = tmp_path / "source"
        src.mkdir()
        _git(src, "init", "--initial-branch=main")
        _git(src, "config", "user.email", "test@test.com")
        _git(src, "config", "user.name", "Test")
        (src / "file1.txt").write_text("commit 1\n")
        _git(src, "add", ".")
        _git(src, "commit", "-m", "commit 1")
        first_sha = _git(src, "rev-parse", "HEAD")

        (src / "file2.txt").write_text("commit 2\n")
        _git(src, "add", ".")
        _git(src, "commit", "-m", "commit 2")

        # 2. Create a shallow bare clone (depth=1, only HEAD).
        # Use file:// URL to force smart-HTTP-like transport that actually respects
        # --depth (plain local paths use hardlink transport that copies all packs).
        bare = tmp_path / "bare"
        file_url = src.as_uri()
        subprocess.run(
            ["git", "clone", "--bare", "--depth=1", file_url, str(bare)],
            check=True,
            capture_output=True,
        )

        # Verify first_sha is NOT in the bare (depth=1 via file:// properly excludes parents)
        verify = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"{first_sha}^{{commit}}"],
            capture_output=True,
        )
        assert verify.returncode != 0, "first_sha should NOT be in depth=1 bare"

        # 3. Call fetch_sha_into_bare -- execute_transport_plan calls the fetch
        # action with the local src path so no actual network access is needed.
        dep_ref = DependencyReference.parse("owner/repo/sub#main")
        result = fetch_sha_into_bare(
            _make_execute(src),
            file_url,
            bare,
            first_sha,
            dep_ref=dep_ref,
        )

        # 4. Assert success
        assert result is True

        # 5. Assert pin ref exists
        refs = subprocess.run(
            [
                "git",
                "--git-dir",
                str(bare),
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads/apm-pin-*",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        expected_ref = f"refs/heads/apm-pin-{first_sha[:12]}"
        assert expected_ref in refs, f"Expected {expected_ref} in refs, got: {refs!r}"

        # 6. Assert SHA is now accessible
        verify2 = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"{first_sha}^{{commit}}"],
            capture_output=True,
        )
        assert verify2.returncode == 0, "first_sha should now be accessible after fetch"

    def test_already_present_sha_gets_pinned(self, tmp_path: Path) -> None:
        """When SHA is already present (full clone), execute is skipped and pin ref is created."""
        # Create source repo with 1 commit
        src = tmp_path / "source"
        src.mkdir()
        _git(src, "init", "--initial-branch=main")
        _git(src, "config", "user.email", "test@test.com")
        _git(src, "config", "user.name", "Test")
        (src / "file.txt").write_text("content\n")
        _git(src, "add", ".")
        _git(src, "commit", "-m", "initial")
        sha = _git(src, "rev-parse", "HEAD")

        # Full bare clone (SHA is present)
        bare = tmp_path / "bare"
        subprocess.run(
            ["git", "clone", "--bare", str(src), str(bare)],
            check=True,
            capture_output=True,
        )

        # Verify SHA IS already in the full bare
        verify = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"{sha}^{{commit}}"],
            capture_output=True,
        )
        assert verify.returncode == 0, "SHA should be present in full bare"

        dep_ref = DependencyReference.parse("owner/repo/sub#main")
        execute_calls: list[int] = []

        def counting_execute(
            url: str,
            target: Path,
            *,
            dep_ref: DependencyReference,
            clone_action: Callable[..., None],
            **kwargs: Any,
        ) -> None:
            execute_calls.append(1)

        result = fetch_sha_into_bare(
            counting_execute,
            str(src),
            bare,
            sha,
            dep_ref=dep_ref,
        )

        assert result is True
        # execute should NOT be called (SHA already present)
        assert execute_calls == [], "execute_transport_plan must not be called when SHA is present"

        # Pin ref should exist
        refs = subprocess.run(
            [
                "git",
                "--git-dir",
                str(bare),
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads/apm-pin-*",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        expected_ref = f"refs/heads/apm-pin-{sha[:12]}"
        assert expected_ref in refs, f"Expected {expected_ref} in refs, got: {refs!r}"
