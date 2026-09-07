"""CLI input validation and direct cache API mutation protection."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cache.git_cache import GitCache
from apm_cli.commands.cache import cache
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = pytest.mark.component


@pytest.mark.parametrize("days", [-1, -30])
@pytest.mark.parametrize("populated", [False, True])
def test_api_rejects_negative_age_without_mutation(
    tmp_path: Path, days: int, populated: bool
) -> None:
    root = tmp_path / "cache"
    if populated:
        checkout = root / "git" / "checkouts_v1" / "repo" / ("a" * 40)
        checkout.mkdir(parents=True)
        (checkout / "sentinel").write_bytes(b"preserve cached content")
    git_cache = GitCache(root)
    before = ArtifactSnapshot.capture(root)
    with pytest.raises(ValueError, match="max_age_days must be nonnegative"):
        git_cache.prune(max_age_days=days)
    assert_unchanged(before, ArtifactSnapshot.capture(root))


@pytest.mark.parametrize("days", ["-1", "-30", "not-an-integer"])
def test_cli_rejects_invalid_age_before_creating_cache(tmp_path: Path, days: str) -> None:
    root = tmp_path / "absent-cache"
    result = CliRunner().invoke(
        cache, ["prune", "--days", days], env={"APM_CACHE_DIR": str(root), "APM_NO_CACHE": "0"}
    )
    assert result.exit_code == 2
    assert "--days" in result.output
    assert "Pruned" not in result.output
    assert not root.exists()
