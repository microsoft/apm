"""Mutation coverage for the semantic repository cache identity owner guard."""

from __future__ import annotations

from pathlib import Path

from scripts.check_repository_cache_identity_owner import (
    SHARED_CACHE_PATH,
    TIERED_RESOLVER_PATH,
    check,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests/fixtures/architecture/repository_cache_identity"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _mutation_root(
    tmp_path: Path,
    *,
    shared_source: str,
    tiered_source: str,
) -> Path:
    shared = tmp_path / SHARED_CACHE_PATH
    tiered = tmp_path / TIERED_RESOLVER_PATH
    shared.parent.mkdir(parents=True)
    shared.write_text(shared_source, encoding="utf-8")
    tiered.write_text(tiered_source, encoding="utf-8")
    return tmp_path


def test_current_repository_cache_identity_owners_are_clean() -> None:
    assert check(ROOT) == []


def test_shared_post_normalization_truncation_fixture_fails(tmp_path: Path) -> None:
    root = _mutation_root(
        tmp_path,
        shared_source=_source(FIXTURES / "shared_post_normalization_truncation.py.txt"),
        tiered_source=_source(ROOT / TIERED_RESOLVER_PATH),
    )
    violations = check(root)

    assert any("without post-normalization transforms" in item.message for item in violations)


def test_tiered_l0_indirect_truncation_fixture_fails(tmp_path: Path) -> None:
    root = _mutation_root(
        tmp_path,
        shared_source=_source(ROOT / SHARED_CACHE_PATH),
        tiered_source=_source(FIXTURES / "tiered_l0_indirect_truncation.py.txt"),
    )
    violations = check(root)

    assert any("without indirect truncation" in item.message for item in violations)


def test_tiered_l0_keyword_truncation_fixture_fails(tmp_path: Path) -> None:
    root = _mutation_root(
        tmp_path,
        shared_source=_source(ROOT / SHARED_CACHE_PATH),
        tiered_source=_source(FIXTURES / "tiered_l0_keyword_truncation.py.txt"),
    )
    violations = check(root)

    assert any("without indirect truncation" in item.message for item in violations)


def test_missing_configured_owner_path_fails_closed(tmp_path: Path) -> None:
    assert check(tmp_path)
