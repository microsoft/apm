"""Unit tests for the virtual self-entry synthesis in LockFile.

Covers:
- _SELF_KEY constant
- LockFile.from_yaml() synthesizes a "." entry from local_deployed_files
- LockFile.to_yaml() does NOT emit the "." entry into the dependencies array
- Round-trip byte-stability of the YAML output
- get_package_dependencies() excludes the self-entry
- get_all_dependencies() includes the self-entry, sorted first by depth=0
- get_unique_key() returns "." for the synthesized entry
- is_semantically_equivalent honors the synthesized entry
"""

import yaml

from apm_cli.deps.lockfile import (
    LockFile,
    LockedDependency,
    _SELF_KEY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lockfile_with_local_content() -> LockFile:
    """Build a LockFile with realistic local content + one remote dep."""
    lock = LockFile(
        lockfile_version="1",
        generated_at="2025-01-01T00:00:00+00:00",
        apm_version="0.0.0-test",
    )
    # A real remote dependency at depth=1
    lock.add_dependency(
        LockedDependency(
            repo_url="https://github.com/owner/repo",
            resolved_commit="a" * 40,
            depth=1,
            deployed_files=[".github/skills/foo/SKILL.md"],
            deployed_file_hashes={".github/skills/foo/SKILL.md": "deadbeef"},
        )
    )
    # Local content (the "self" payload)
    lock.local_deployed_files = [
        ".github/instructions/python.instructions.md",
        ".github/skills/local/SKILL.md",
        ".apm/agents/main.agent.md",
    ]
    lock.local_deployed_file_hashes = {
        ".github/instructions/python.instructions.md": "1111111111111111",
        ".github/skills/local/SKILL.md": "2222222222222222",
        ".apm/agents/main.agent.md": "3333333333333333",
    }
    return lock


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


def test_self_key_constant_is_dot():
    assert _SELF_KEY == "."


# ---------------------------------------------------------------------------
# Synthesis behavior
# ---------------------------------------------------------------------------


class TestSelfEntrySynthesis:
    def test_synthesized_entry_present_when_local_content(self):
        lock = _make_lockfile_with_local_content()
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        assert _SELF_KEY in roundtripped.dependencies

    def test_synthesized_entry_fields(self):
        lock = _make_lockfile_with_local_content()
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        self_dep = roundtripped.dependencies[_SELF_KEY]
        assert self_dep.is_dev is True
        assert self_dep.source == "local"
        assert self_dep.local_path == "."
        assert self_dep.repo_url == "<self>"
        assert self_dep.depth == 0

    def test_synthesized_entry_carries_deployed_files_and_hashes(self):
        lock = _make_lockfile_with_local_content()
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        self_dep = roundtripped.dependencies[_SELF_KEY]
        assert sorted(self_dep.deployed_files) == sorted(lock.local_deployed_files)
        assert self_dep.deployed_file_hashes == lock.local_deployed_file_hashes

    def test_no_self_entry_when_local_deployed_files_empty(self):
        lock = LockFile(
            lockfile_version="1",
            generated_at="2025-01-01T00:00:00+00:00",
        )
        # No local content, no remote deps
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        assert _SELF_KEY not in roundtripped.dependencies

    def test_no_self_entry_when_only_remote_deps(self):
        lock = LockFile(
            lockfile_version="1",
            generated_at="2025-01-01T00:00:00+00:00",
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="https://github.com/owner/repo",
                resolved_commit="b" * 40,
                depth=1,
            )
        )
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        assert _SELF_KEY not in roundtripped.dependencies

    def test_synthesized_entry_unique_key_is_dot(self):
        lock = _make_lockfile_with_local_content()
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        self_dep = roundtripped.dependencies[_SELF_KEY]
        assert self_dep.get_unique_key() == "."


# ---------------------------------------------------------------------------
# YAML serialization: self-entry must NOT appear in the dependencies array
# ---------------------------------------------------------------------------


class TestSelfEntryNotSerialized:
    def test_self_entry_absent_from_dependencies_array(self):
        lock = _make_lockfile_with_local_content()
        # Force the in-memory presence of the synthesized self-entry first.
        lock = LockFile.from_yaml(lock.to_yaml())
        assert _SELF_KEY in lock.dependencies  # precondition

        out = lock.to_yaml()
        parsed = yaml.safe_load(out)
        repo_urls = [d["repo_url"] for d in parsed.get("dependencies", [])]
        assert "<self>" not in repo_urls
        # Local content is still preserved via the flat fields
        assert parsed["local_deployed_files"] == sorted(lock.local_deployed_files)

    def test_to_yaml_restores_self_entry_in_memory(self):
        """to_yaml must not mutate the in-memory dependencies dict (try/finally)."""
        lock = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        assert _SELF_KEY in lock.dependencies
        _ = lock.to_yaml()
        assert _SELF_KEY in lock.dependencies, (
            "to_yaml() must restore the popped self-entry"
        )

    def test_to_yaml_restores_self_entry_even_on_exception(self, monkeypatch):
        """If serialization raises, the self-entry must still be restored."""
        from apm_cli.utils import yaml_io

        lock = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        assert _SELF_KEY in lock.dependencies

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated dump failure")

        monkeypatch.setattr(yaml_io, "yaml_to_str", _boom)
        try:
            lock.to_yaml()
        except RuntimeError:
            pass
        assert _SELF_KEY in lock.dependencies, (
            "to_yaml() must restore self-entry even on exception"
        )


# ---------------------------------------------------------------------------
# Round-trip byte stability (the critical correctness invariant)
# ---------------------------------------------------------------------------


class TestRoundTripByteStability:
    def test_round_trip_bytes_stable_with_local_content(self):
        lock = _make_lockfile_with_local_content()
        # First dump establishes a canonical YAML form.
        canonical = lock.to_yaml()
        # Reload + redump should produce a byte-identical string.
        reloaded = LockFile.from_yaml(canonical)
        redumped = reloaded.to_yaml()
        assert canonical == redumped

    def test_round_trip_bytes_stable_no_local_content(self):
        lock = LockFile(
            lockfile_version="1",
            generated_at="2025-01-01T00:00:00+00:00",
            apm_version="0.0.0-test",
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="https://github.com/owner/repo",
                resolved_commit="c" * 40,
                depth=1,
            )
        )
        canonical = lock.to_yaml()
        redumped = LockFile.from_yaml(canonical).to_yaml()
        assert canonical == redumped

    def test_multiple_round_trips_remain_stable(self):
        lock = _make_lockfile_with_local_content()
        y1 = lock.to_yaml()
        y2 = LockFile.from_yaml(y1).to_yaml()
        y3 = LockFile.from_yaml(y2).to_yaml()
        assert y1 == y2 == y3


# ---------------------------------------------------------------------------
# get_all_dependencies / get_package_dependencies
# ---------------------------------------------------------------------------


class TestDependencyAccessors:
    def test_get_all_dependencies_includes_self_entry(self):
        lock = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        all_deps = lock.get_all_dependencies()
        assert any(d.local_path == "." for d in all_deps)

    def test_get_all_dependencies_self_entry_sorted_first(self):
        lock = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        all_deps = lock.get_all_dependencies()
        # depth=0 sorts first by (depth, repo_url)
        assert all_deps[0].local_path == "."
        assert all_deps[0].depth == 0

    def test_get_package_dependencies_excludes_self_entry(self):
        lock = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        pkg_deps = lock.get_package_dependencies()
        assert all(d.local_path != "." for d in pkg_deps)
        # Should still contain the remote dep
        assert any(d.repo_url == "https://github.com/owner/repo" for d in pkg_deps)

    def test_get_package_dependencies_empty_when_only_self(self):
        lock = LockFile(
            lockfile_version="1",
            generated_at="2025-01-01T00:00:00+00:00",
        )
        lock.local_deployed_files = [".github/skills/local/SKILL.md"]
        lock.local_deployed_file_hashes = {".github/skills/local/SKILL.md": "abc"}
        roundtripped = LockFile.from_yaml(lock.to_yaml())
        assert _SELF_KEY in roundtripped.dependencies
        assert roundtripped.get_package_dependencies() == []


# ---------------------------------------------------------------------------
# is_semantically_equivalent
# ---------------------------------------------------------------------------


class TestSemanticEquivalenceWithSelfEntry:
    def test_two_lockfiles_with_same_local_content_equivalent(self):
        lock_a = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        lock_b = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())
        assert lock_a.is_semantically_equivalent(lock_b)
        assert lock_b.is_semantically_equivalent(lock_a)

    def test_different_local_content_not_equivalent(self):
        lock_a = LockFile.from_yaml(_make_lockfile_with_local_content().to_yaml())

        other = _make_lockfile_with_local_content()
        other.local_deployed_files = list(other.local_deployed_files) + [
            ".github/skills/extra/SKILL.md"
        ]
        other.local_deployed_file_hashes = dict(other.local_deployed_file_hashes)
        other.local_deployed_file_hashes[".github/skills/extra/SKILL.md"] = "ffff"
        lock_b = LockFile.from_yaml(other.to_yaml())

        assert not lock_a.is_semantically_equivalent(lock_b)
