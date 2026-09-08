"""Static regression proofs for the cache-clean outcome owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE = "transport-platform-cache-cleanup-outcome"


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/cache/http_cache.py",
            "return clean_cache_buckets((self._cache_dir,))",
            "return []",
        ),
        (
            "src/apm_cli/cache/git_cache.py",
            "return clean_cache_buckets((self._db_root, self._checkouts_root))",
            "return clean_cache_buckets((self._db_root, self._checkouts_root))\n\n"
            "def clean_cache_buckets(buckets):\n    return []\n",
        ),
        (
            "src/apm_cli/cache/cleanup.py",
            "path.lstat()",
            "path.stat()",
        ),
        (
            "src/apm_cli/commands/cache.py",
            "failures.extend(cache_type(root).clean_all())",
            "cache_type(root).clean_all()",
        ),
    ],
)
def test_cache_cleanup_guard_rejects_split_or_discarded_outcomes(
    path: str, old: str, new: str
) -> None:
    """A second authority or an ignored owner result must fail the registered guard."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: source.replace(old, new, 1)})
    assert report.failures == ()
    assert any(violation.rule_id == RULE for violation in report.violations)


def test_cache_cleanup_guard_accepts_canonical_routing() -> None:
    """The rule is silent for production routing before mutation."""
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()
