"""Real-git regression: package links materialize from Git objects (#3457).

Dependency checkouts pin ``core.symlinks=false`` so Git never needs OS
symlink capability, then replace each tracked link with its target's
content. Every host therefore records the hash a POSIX checkout records
when it follows the links, even when the caller's config asks for real
symlinks and even on Windows without Developer Mode.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import (
    GitCache,
    _checkout_has_materialization_pins,
    _safe_git_args,
)
from apm_cli.deps.bare_cache import materialize_from_bare
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.file_ops import robust_copytree
from apm_cli.utils.git_sparse import validate_materialized_symlinks
from tests.utils.git_link_fixture import (
    EXPECTED_PKG,
    EXPECTED_PKG_EXECUTABLE,
    GOLDEN_PKG_HASH,
    bare_clone,
    build_link_origin,
    executable_files,
    git,
    host_creates_symlinks,
    neutral_git_env,
    tree_bytes,
)

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


def _caller_env_asking_for_real_symlinks() -> dict[str, str]:
    """A frozen caller config that would make Git create OS symlinks."""
    env = neutral_git_env()
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.symlinks", GIT_CONFIG_VALUE_0="true")
    return env


def _install(checkout: Path, tmp_path: Path) -> Path:
    installed = tmp_path / "apm_modules" / "pkg"
    robust_copytree(checkout / "pkg", installed)
    return installed


def test_safe_git_args_pin_link_text() -> None:
    args = _safe_git_args()
    assert "core.symlinks=false" in args
    assert args[args.index("core.symlinks=false") - 1] == "-c"


def test_posix_follow_links_baseline_is_the_golden_hash(tmp_path: Path) -> None:
    """Today's POSIX result: real symlinks followed by the package copy."""
    if not host_creates_symlinks(tmp_path / "capability"):
        pytest.skip("The POSIX baseline needs a host that creates symlinks")
    origin, _ = build_link_origin(tmp_path)
    posix = tmp_path / "posix"
    git(
        "-c",
        "core.symlinks=true",
        "-c",
        "core.autocrlf=false",
        "clone",
        "-q",
        str(origin),
        str(posix),
    )
    assert (posix / "pkg" / "AGENTS.md").is_symlink()
    installed = tmp_path / "installed"
    shutil.copytree(posix / "pkg", installed)

    assert tree_bytes(installed) == EXPECTED_PKG
    assert compute_package_hash(installed) == GOLDEN_PKG_HASH


@pytest.mark.parametrize("sparse_paths", [None, ["pkg"]], ids=["full", "sparse"])
def test_cached_checkout_materializes_links_with_golden_hash(
    tmp_path: Path, sparse_paths: list[str] | None
) -> None:
    origin, sha = build_link_origin(tmp_path)
    cache = GitCache(tmp_path / "cache")

    checkout = cache.get_checkout(
        str(origin),
        sha,
        locked_sha=sha,
        env=_caller_env_asking_for_real_symlinks(),
        sparse_paths=sparse_paths,
    )
    installed = _install(checkout, tmp_path)

    assert tree_bytes(installed) == EXPECTED_PKG
    assert compute_package_hash(installed) == GOLDEN_PKG_HASH
    if os.name != "nt":
        assert executable_files(installed) == EXPECTED_PKG_EXECUTABLE
    assert _checkout_has_materialization_pins(checkout)
    if sparse_paths:
        # The out-of-cone target came from Git objects; the cone never widened.
        assert not (checkout / "shared").exists()


def test_cache_never_creates_os_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Materialization needs no OS symlink capability (#3457)."""

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", denied)
    origin, sha = build_link_origin(tmp_path)

    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), sha, locked_sha=sha)

    assert compute_package_hash(_install(checkout, tmp_path)) == GOLDEN_PKG_HASH
    assert not any(path.is_symlink() for path in checkout.rglob("*"))


@pytest.mark.parametrize("legacy", ["link-text", "no-marker"])
def test_cache_hit_rematerializes_shard_without_link_materialization(
    tmp_path: Path, legacy: str
) -> None:
    """Shards from before #3457 hold OS links or link text; heal them on hit."""
    origin, sha = build_link_origin(tmp_path)
    cache = GitCache(tmp_path / "cache")
    stale = cache.get_checkout(str(origin), sha, locked_sha=sha)
    agents = stale / "pkg" / "AGENTS.md"
    agents.write_bytes(b"CLAUDE.md")
    config = stale / ".git" / "config"
    text = config.read_text(encoding="utf-8")
    if legacy == "link-text":
        text = text.replace("[apm]", "[apm-legacy]")
    else:
        text = text.replace("symlinks = false", "symlinks = true")
    config.write_text(text, encoding="utf-8")
    assert not _checkout_has_materialization_pins(stale)

    healed = cache.get_checkout(str(origin), sha, locked_sha=sha)

    assert agents.read_bytes() == b"Package instructions\n"
    assert _checkout_has_materialization_pins(healed)


def test_pins_require_line_endings_link_text_and_marker(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    config = checkout / ".git" / "config"
    complete = (
        "[core]\n\tautocrlf = false\n\teol = lf\n\tsymlinks = false\n"
        "[apm]\n\tlinkMaterialization = git-objects\n"
    )
    config.write_text(complete, encoding="utf-8")
    assert _checkout_has_materialization_pins(checkout)
    for missing in (
        "\tautocrlf = false\n",
        "\teol = lf\n",
        "\tsymlinks = false\n",
        "\tlinkMaterialization",
    ):
        config.write_text(
            complete.replace(missing, "\tunrelated = 1\n" if missing.endswith("\n") else "\tother"),
            encoding="utf-8",
        )
        assert not _checkout_has_materialization_pins(checkout)
    for wrong in (("symlinks = false", "symlinks = true"), ("eol = lf", "eol = crlf")):
        config.write_text(complete.replace(*wrong), encoding="utf-8")
        assert not _checkout_has_materialization_pins(checkout)


def test_shard_keeps_unresolvable_link_for_the_consumer_to_reject(tmp_path: Path) -> None:
    """A shared shard is lenient; the package consumer applies the strict check."""
    origin, sha = build_link_origin(tmp_path, {"pkg/escape.md": "../../outside.md"})

    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), sha, locked_sha=sha)

    assert (checkout / "pkg" / "escape.md").read_bytes() == b"../../outside.md"
    assert (checkout / "pkg" / "AGENTS.md").read_bytes() == b"Package instructions\n"
    with pytest.raises(ValueError, match=r"pkg/escape\.md.*outside the repository"):
        validate_materialized_symlinks("git", checkout, ["pkg"], env=neutral_git_env())


def test_bare_materialization_resolves_links_over_caller_env(tmp_path: Path) -> None:
    """The shared-bare consumer checkout path materializes the same bytes."""
    origin, sha = build_link_origin(tmp_path)
    bare = bare_clone(origin, tmp_path / "bare.git")
    consumer = tmp_path / "consumer"

    materialize_from_bare(
        bare,
        consumer,
        ref=sha,
        env=_caller_env_asking_for_real_symlinks(),
        known_sha=sha,
        sparse_paths=["pkg"],
    )

    installed = _install(consumer, tmp_path)
    assert tree_bytes(installed) == EXPECTED_PKG
    assert compute_package_hash(installed) == GOLDEN_PKG_HASH
