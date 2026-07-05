"""T3: the #2005 docs/CHANGELOG must scope OS-trust honestly.

Round-1 shipped copy claiming ``apm run`` child runtimes (incl. ``codex``)
re-run the OS-trust bootstrap. That was a field no-op for the ``llm`` venv and
never true for the Node/Rust runtimes. These tests are the silent-drift guard
that keeps the prose scoped to what actually ships: ``apm install`` plus the
Python ``llm`` runtime, with Node (Copilot) / Rust (Codex) tracked in #2034.
"""

from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def _unreleased_block(changelog: str) -> str:
    start = changelog.index("## [Unreleased]")
    rest = changelog[start + len("## [Unreleased]") :]
    end = rest.find("\n## [")
    return rest if end == -1 else rest[:end]


def test_changelog_scopes_os_trust_and_references_followup():
    changelog = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    block = _unreleased_block(changelog)

    # Follow-up issue for the uncovered runtimes must be cited.
    assert "#2034" in block, "CHANGELOG must reference the Node/Rust follow-up (#2034)"
    # The honest scope: llm runtime named, Node/Codex explicitly not-yet-covered.
    assert "`llm`" in block
    assert "not yet covered" in block
    # The stale round-1 joint claim must be gone.
    assert "and `apm run` (child runtimes)" not in block


def test_ssl_docs_scope_and_known_limitations():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    assert "### Known limitations" in docs, "ssl-issues.md must have a Known limitations section"
    assert "#2034" in docs, "ssl-issues.md must reference the Node/Rust follow-up (#2034)"
    # Node (Copilot) / Rust (Codex) must be described as NOT covered.
    assert "not yet covered" in docs
    # The stale round-1 claim that codex re-runs the bootstrap must be gone.
    assert "the `llm` and `codex` CLIs) re-run the same OS-trust bootstrap" not in docs
