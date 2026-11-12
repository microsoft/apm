"""Real-Git regression coverage for out-of-cone package links (#2707, #3457)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.cache.git_cache import GitCache, _variant_key
from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.git_sparse import apply_sparse_cone, validate_materialized_symlinks
from apm_cli.utils.path_security import PathTraversalError

pytestmark = pytest.mark.component


def _commit_symlink_repo(
    tmp_path: Path,
    target: str,
    *,
    package_path: str = "packages/tool",
) -> Path:
    """Create a bare repo with a tracked symlink inside the package cone."""
    work = tmp_path / "work"
    package = work / package_path
    shared = work / "shared"
    package.mkdir(parents=True)
    shared.mkdir()
    (package / "apm.yml").write_text("name: tool\nversion: 1.0.0\n", encoding="utf-8")
    (shared / "reference.md").write_text("shared content\n", encoding="utf-8")
    (package / "reference.md").symlink_to(target)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "APM Test"], check=True)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "fixture"], check=True)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


def _checkout_sparse(tmp_path: Path, bare: Path, consumer: Path | None = None) -> Path:
    consumer = consumer or tmp_path / "consumer"
    consumer.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", str(bare), str(consumer)],
        check=True,
    )
    apply_sparse_cone("git", consumer, ["packages/tool"], env=os.environ.copy())
    subprocess.run(["git", "-C", str(consumer), "checkout", "-q", "HEAD"], check=True)
    return consumer


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_legacy_downloader_materializes_out_of_cone_symlink(tmp_path: Path) -> None:
    """The no-cache downloader path installs the out-of-cone target's content."""
    bare = _commit_symlink_repo(tmp_path, "../../shared/reference.md")
    checkout = tmp_path / "legacy"
    downloader = object.__new__(GitHubPackageDownloader)
    downloader.git_env = {}
    downloader.github_token = None
    downloader.auth_resolver = MagicMock()
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = False
    downloader._resolve_dep_auth_ctx = lambda dep: None
    downloader._build_repo_url = lambda *args, **kwargs: str(bare)
    dep = DependencyReference(repo_url="owner/repo", reference="main")

    assert downloader._try_sparse_checkout(dep, checkout, "packages/tool", "main") is True
    installed_link = checkout / "packages" / "tool" / "reference.md"
    assert not installed_link.is_symlink()
    assert installed_link.read_text(encoding="utf-8") == "shared content\n"
    # Resolved from Git objects: the sparse cone never widened.
    assert not (checkout / "shared").exists()


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_validation_rejects_link_that_is_broken(tmp_path: Path) -> None:
    """A link whose target is absent from the commit is explained, not copied."""
    bare = _commit_symlink_repo(tmp_path, "../../missing/reference.md")
    consumer = _checkout_sparse(tmp_path, bare)

    with pytest.raises(RuntimeError, match="is not a tracked file or directory"):
        validate_materialized_symlinks(
            "git",
            consumer,
            ["packages/tool"],
            env=os.environ.copy(),
        )


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_materialization_rejects_symlink_outside_checkout(tmp_path: Path) -> None:
    """Remote package copies must remain within the pinned checkout."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    bare = _commit_symlink_repo(tmp_path, str(outside))
    consumer = _checkout_sparse(tmp_path, bare)

    with pytest.raises(PathTraversalError, match="resolves outside the repository"):
        validate_materialized_symlinks(
            "git",
            consumer,
            ["packages/tool"],
            env=os.environ.copy(),
        )


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_persistent_cache_hit_heals_preexisting_dangling_shard(tmp_path: Path) -> None:
    """A cache variant created before #3457 is rematerialized when reused."""
    bare = _commit_symlink_repo(tmp_path, "../../shared/reference.md")
    sha = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    cache_root = tmp_path / "cache"
    cache = GitCache(cache_root)
    url = bare.as_uri()
    checkout = (
        cache_root
        / "git"
        / "checkouts_v1"
        / cache_shard_key(url)
        / sha
        / _variant_key(["packages/tool"])
    )
    _checkout_sparse(tmp_path, bare, checkout)
    link = checkout / "packages" / "tool" / "reference.md"
    assert link.is_symlink()
    assert not link.exists()

    result = cache.get_checkout(
        url,
        "main",
        locked_sha=sha,
        env=os.environ.copy(),
        sparse_paths=["packages/tool"],
    )

    assert result == checkout
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "shared content\n"


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_invalid_symlink_lands_lenient_shard_for_consumer_rejection(tmp_path: Path) -> None:
    """A shared shard keeps the escaping link as text; the consumer rejects it."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    bare = _commit_symlink_repo(tmp_path, str(outside))
    sha = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    cache_root = tmp_path / "cache"
    cache = GitCache(cache_root)

    checkout = cache.get_checkout(
        bare.as_uri(),
        "main",
        locked_sha=sha,
        env=os.environ.copy(),
        sparse_paths=["packages/tool"],
    )

    checkout_root = cache_root / "git" / "checkouts_v1"
    assert list(checkout_root.rglob("*.inc.*")) == []
    link = checkout / "packages" / "tool" / "reference.md"
    assert link.read_text(encoding="utf-8") == str(outside)
    with pytest.raises(PathTraversalError, match="resolves outside the repository"):
        validate_materialized_symlinks("git", checkout, ["packages/tool"], env=os.environ.copy())


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_downloader_rejects_real_symlink_into_git_metadata(tmp_path: Path) -> None:
    """The user-facing downloader must reject generated Git metadata targets."""
    bare = _commit_symlink_repo(tmp_path, "../../.git/config")
    consumer = _checkout_sparse(tmp_path, bare)
    downloader = object.__new__(GitHubPackageDownloader)
    downloader.install_logger = None
    downloader.shared_clone_cache = None
    downloader.auth_resolver = MagicMock()
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = False
    downloader.persistent_git_cache = MagicMock()
    downloader.persistent_git_cache.get_checkout.return_value = consumer
    downloader.resolve_git_reference = lambda dep: MagicMock(resolved_commit="a" * 40)
    downloader._cache_git_env = lambda dep: os.environ.copy()
    downloader._git_env_dict = lambda: os.environ.copy()
    dep = DependencyReference(
        repo_url="owner/repo",
        reference="main",
        is_virtual=True,
        virtual_path="packages/tool",
    )
    target = tmp_path / "installed"

    with pytest.raises(RuntimeError, match="is not a tracked file or directory"):
        downloader.download_subdirectory_package(dep, target)

    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="The fixture commits links through OS symlinks")
def test_downloader_treats_colon_prefixed_package_path_literally(tmp_path: Path) -> None:
    """Git pathspec magic must not hide an external package symlink."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    package_path = ":(literal)pkg"
    bare = _commit_symlink_repo(
        tmp_path,
        str(outside),
        package_path=package_path,
    )
    consumer = tmp_path / "consumer"
    subprocess.run(["git", "clone", "-q", str(bare), str(consumer)], check=True)
    downloader = object.__new__(GitHubPackageDownloader)
    downloader.install_logger = None
    downloader.shared_clone_cache = None
    downloader.auth_resolver = MagicMock()
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = False
    downloader.persistent_git_cache = MagicMock()
    downloader.persistent_git_cache.get_checkout.return_value = consumer
    downloader.resolve_git_reference = lambda dep: MagicMock(resolved_commit="a" * 40)
    downloader._cache_git_env = lambda dep: os.environ.copy()
    downloader._git_env_dict = lambda: os.environ.copy()
    dep = DependencyReference(
        repo_url="owner/repo",
        reference="main",
        is_virtual=True,
        virtual_path=package_path,
    )

    with pytest.raises(PathTraversalError, match="resolves outside the repository"):
        downloader.download_subdirectory_package(dep, tmp_path / "installed")
