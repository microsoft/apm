"""Coverage tests for ``compute_deployed_hashes`` (issue #887, Wave 3).

The lockfile self-entry synthesis (Wave 1) made it visible that
``local_deployed_files`` and ``local_deployed_file_hashes`` can legitimately
have different cardinalities: directory entries are tracked in the file list
(for cleanup/audit) but never hashed (only regular file contents have
meaningful provenance).

These tests pin down that contract so future changes don't accidentally:

* Drop hashes for regular files (silent audit blindness).
* Add hashes for directories or symlinks (false provenance claims).

The architect's section 1.6 edge cases (directories, symlinks, empty files,
hidden files) are exercised against a synthesized fixture project that
mirrors the real ``.apm/`` shape this repo emits.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from apm_cli.install.phases.lockfile import compute_deployed_hashes

_SHA256_PREFIXED = re.compile(r"^sha256:[0-9a-f]{64}$")


def _build_fixture_project(root: Path) -> tuple[list[str], set[str], set[str]]:
    """Create a fixture project with a representative .apm/-shaped layout.

    Returns ``(deployed_files, expected_dirs, expected_files)`` where
    ``deployed_files`` is the list a real integrator would emit (mix of
    regular files and a few directory entries, mirroring the live
    behavior observed in this repo: skill subtrees are tracked as a
    single directory entry).

    All paths are POSIX-relative to ``root``.
    """
    apm_dir = root / ".apm"
    skills_dir = root / ".github" / "skills"
    prompts_dir = root / ".github" / "prompts"
    apm_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    prompts_dir.mkdir(parents=True)

    files: list[tuple[str, bytes]] = [
        (".apm/agents/sample.agent.md", b"# sample agent\n"),
        (".apm/skills/devx/SKILL.md", b"# devx skill\n"),
        (".github/prompts/build.prompt.md", b"prompt body\n"),
        (".github/prompts/empty.prompt.md", b""),
        (".mcp.json", b"{}\n"),
    ]
    skill_a = skills_dir / "skill-a"
    skill_b = skills_dir / "skill-b"
    skill_a.mkdir()
    skill_b.mkdir()
    (skill_a / "SKILL.md").write_bytes(b"# skill a\n")
    (skill_b / "SKILL.md").write_bytes(b"# skill b\n")

    expected_files: set[str] = set()
    for rel, payload in files:
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(payload)
        expected_files.add(rel)
    expected_files.update(
        {
            ".github/skills/skill-a/SKILL.md",
            ".github/skills/skill-b/SKILL.md",
        }
    )

    expected_dirs: set[str] = {
        ".github/skills/skill-a",
        ".github/skills/skill-b",
    }

    deployed_files = sorted(expected_files | expected_dirs)
    return deployed_files, expected_dirs, expected_files


class TestComputeDeployedHashesCoverage:
    """Verify the file/hash coverage invariants for a synthesized project."""

    def test_every_regular_file_has_hash_entry(self, tmp_path: Path) -> None:
        deployed_files, _expected_dirs, expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        missing = expected_files - set(hashes.keys())
        assert missing == set(), f"regular files missing from hashes: {sorted(missing)}"

    def test_directories_excluded_from_hashes(self, tmp_path: Path) -> None:
        deployed_files, expected_dirs, _expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        leaked = expected_dirs & set(hashes.keys())
        assert leaked == set(), f"directory entries leaked into hashes: {sorted(leaked)}"

    def test_set_difference_equals_directory_entries(self, tmp_path: Path) -> None:
        """The canonical invariant: ``files - hashes.keys() == dirs``."""
        deployed_files, expected_dirs, _expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        diff = set(deployed_files) - set(hashes.keys())
        assert diff == expected_dirs, (
            f"unexpected coverage gap: diff={sorted(diff)} expected_dirs={sorted(expected_dirs)}"
        )

    def test_hash_values_are_sha256_hex_64chars(self, tmp_path: Path) -> None:
        deployed_files, _expected_dirs, _expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        assert hashes, "fixture must produce at least one hash"
        for rel, value in hashes.items():
            assert _SHA256_PREFIXED.match(value), (
                f"hash for {rel!r} not in sha256:<64hex> form: {value!r}"
            )

    def test_empty_files_are_hashed(self, tmp_path: Path) -> None:
        """Zero-byte files are still regular files and must be hashed."""
        deployed_files, _expected_dirs, _expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        rel = ".github/prompts/empty.prompt.md"
        assert rel in hashes, f"empty file {rel!r} missing from hashes (regular file coverage gap)"
        # SHA-256 of empty bytes is well-known.
        assert hashes[rel] == (
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_hidden_files_are_hashed(self, tmp_path: Path) -> None:
        """Dotfiles like ``.mcp.json`` must not be silently skipped."""
        deployed_files, _expected_dirs, _expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        assert ".mcp.json" in hashes, "hidden dotfile coverage gap: .mcp.json not hashed"

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="symlink creation requires elevated privileges on Windows",
    )
    def test_symlinks_excluded_from_hashes(self, tmp_path: Path) -> None:
        """Documented decision: symlinks are NEVER hashed.

        ``compute_deployed_hashes`` filters via ``is_file() and not
        is_symlink()`` (see ``src/apm_cli/install/phases/lockfile.py:41``)
        and ``compute_file_hash`` itself short-circuits symlinks to the
        empty-content sentinel (see ``src/apm_cli/utils/content_hash.py:77``).
        Provenance over a symlink target would be misleading because the
        target may live outside the project root.

        If a future change starts hashing symlinks, the audit chain
        (``_check_content_integrity``) would silently begin reporting
        target-content provenance for paths that look like local files
        -- a safety regression. Update this test together with that
        decision and document it in lockfile-spec.md.
        """
        _build_fixture_project(tmp_path)
        target = tmp_path / ".apm" / "agents" / "sample.agent.md"
        link = tmp_path / ".apm" / "agents" / "alias.agent.md"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation not supported here: {exc}")

        rel_link = ".apm/agents/alias.agent.md"
        hashes = compute_deployed_hashes([rel_link], tmp_path)
        assert rel_link not in hashes, "symlink leaked into hashes -- contract violated"

    def test_no_extra_hashes_beyond_deployed_files(self, tmp_path: Path) -> None:
        """Hash dict must never contain keys absent from ``deployed_files``."""
        deployed_files, _expected_dirs, _expected_files = _build_fixture_project(tmp_path)
        hashes = compute_deployed_hashes(deployed_files, tmp_path)
        extras = set(hashes.keys()) - set(deployed_files)
        assert extras == set(), f"hashes contain entries not in deployed_files: {sorted(extras)}"

    def test_missing_files_are_skipped_silently(self, tmp_path: Path) -> None:
        """Paths in ``deployed_files`` that don't exist on disk produce no hash.

        This mirrors the ``compute_deployed_hashes`` contract: files that
        cannot be read contribute no provenance, but they are NOT an
        error here -- the audit layer (``_check_content_integrity``) is
        responsible for surfacing the missing-file diagnostic.
        """
        (tmp_path / ".apm").mkdir()
        present = tmp_path / ".apm" / "present.md"
        present.write_bytes(b"hi\n")
        rels = [".apm/present.md", ".apm/missing.md"]
        hashes = compute_deployed_hashes(rels, tmp_path)
        assert ".apm/present.md" in hashes
        assert ".apm/missing.md" not in hashes
