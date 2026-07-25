"""Tests for per-dependency target selection persistence."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.subsets import _levenshtein_distance


class TestLocalPathDepTargets:
    """Regression tests for issue #1982: targets: ignored on path: deps."""

    def test_local_path_parse_targets(self) -> None:
        dep = DependencyReference.parse_from_dict({"path": "./local", "targets": ["claude"]})

        assert dep.target_subset == ["claude"]
        assert dep.is_local

    def test_local_path_parse_no_targets(self) -> None:
        dep = DependencyReference.parse_from_dict({"path": "./local"})

        assert dep.target_subset is None

    def test_local_path_targets_round_trip(self) -> None:
        entry = {"path": "./local", "targets": ["codex", "claude"]}

        emitted = DependencyReference.parse_from_dict(entry).to_apm_yml_entry()

        assert emitted == {"path": "./local", "targets": ["claude", "codex"]}

    def test_local_path_targets_and_skills_coexist(self) -> None:
        entry = {"path": "./local", "targets": ["claude"], "skills": ["reviewer"]}

        dep = DependencyReference.parse_from_dict(entry)

        assert dep.target_subset == ["claude"]
        assert dep.skill_subset == ["reviewer"]
        assert dep.to_apm_yml_entry() == {
            "path": "./local",
            "skills": ["reviewer"],
            "targets": ["claude"],
        }

    def test_local_path_unknown_target_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown target"):
            DependencyReference.parse_from_dict({"path": "./local", "targets": ["notarealthing"]})

    def test_local_path_parse_alias(self) -> None:
        dep = DependencyReference.parse_from_dict({"path": "./local", "alias": "my-skills"})

        assert dep.alias == "my-skills"
        assert dep.is_local

    def test_local_path_alias_round_trip(self) -> None:
        entry = {"path": "./local", "alias": "my-skills"}

        emitted = DependencyReference.parse_from_dict(entry).to_apm_yml_entry()

        assert emitted == {"path": "./local", "alias": "my-skills"}

    def test_local_path_alias_invalid_chars_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid alias"):
            DependencyReference.parse_from_dict({"path": "./local", "alias": "bad alias!"})

    def test_local_path_unknown_field_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported field"):
            DependencyReference.parse_from_dict({"path": "./local", "target": ["claude"]})


def test_parse_targets_field() -> None:
    dep = DependencyReference.parse_from_dict({"git": "owner/repo", "targets": ["codex"]})

    assert dep.target_subset == ["codex"]


def test_parse_no_targets_field() -> None:
    dep = DependencyReference.parse_from_dict({"git": "owner/repo"})

    assert dep.target_subset is None


def test_parse_targets_sorts_and_dedupes() -> None:
    dep = DependencyReference.parse_from_dict(
        {"git": "owner/repo", "targets": ["Claude", "codex", "claude"]}
    )

    assert dep.target_subset == ["claude", "codex"]


def test_parse_targets_empty_list_raises() -> None:
    with pytest.raises(ValueError, match="targets: must contain at least one target"):
        DependencyReference.parse_from_dict({"git": "owner/repo", "targets": []})


def test_parse_targets_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match=r"Valid targets: .* Did you mean 'codex'"):
        DependencyReference.parse_from_dict({"git": "owner/repo", "targets": ["codx"]})


def test_parse_targets_suggests_name_at_edit_distance_boundary() -> None:
    with pytest.raises(ValueError, match=r"Did you mean 'codex'"):
        DependencyReference.parse_from_dict({"git": "owner/repo", "targets": ["coexx"]})


def test_parse_targets_omits_suggestion_beyond_edit_distance_boundary() -> None:
    with pytest.raises(ValueError, match=r"^((?!Did you mean).)*$"):
        DependencyReference.parse_from_dict({"git": "owner/repo", "targets": ["coexxx"]})


def test_parse_targets_rejects_non_string_name() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        DependencyReference.parse_from_dict({"git": "owner/repo", "targets": [7]})


def test_parse_targets_tie_break_prefers_lexicographically_first_match() -> None:
    # "coade" is distance 2 from both "claude" and "codex"; "claude" sorts first.
    with pytest.raises(ValueError, match=r"Did you mean 'claude'"):
        DependencyReference.parse_from_dict({"git": "owner/repo", "targets": ["coade"]})


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("same", "same", 0),
        ("", "codex", 5),
        ("codex", "", 5),
        ("codexx", "codex", 1),
        ("kitten", "sitting", 3),
        ("flaw", "lawn", 2),
    ],
)
def test_target_edit_distance_contract(left: str, right: str, expected: int) -> None:
    assert _levenshtein_distance(left, right) == expected


def test_to_apm_yml_entry_with_targets() -> None:
    dep = DependencyReference.parse("owner/repo")
    dep.target_subset = ["codex"]

    assert dep.to_apm_yml_entry() == {"git": "owner/repo", "targets": ["codex"]}


def test_to_apm_yml_entry_without_targets_is_string() -> None:
    assert DependencyReference.parse("owner/repo").to_apm_yml_entry() == "owner/repo"


def test_round_trip_parse_emit() -> None:
    entry = {
        "git": "owner/repo",
        "ref": "main",
        "targets": ["codex", "claude"],
    }

    emitted = DependencyReference.parse_from_dict(entry).to_apm_yml_entry()

    assert emitted == {
        "git": "owner/repo",
        "ref": "main",
        "targets": ["claude", "codex"],
    }


def test_targets_and_skills_coexist() -> None:
    dep = DependencyReference.parse_from_dict(
        {
            "git": "owner/repo",
            "skills": ["reviewer"],
            "targets": ["codex"],
        }
    )

    assert dep.to_apm_yml_entry() == {
        "git": "owner/repo",
        "skills": ["reviewer"],
        "targets": ["codex"],
    }


class TestLockedDependencyTargets:
    """Lockfile audit persistence for per-dependency targets."""

    def test_targets_emitted_in_to_dict(self) -> None:
        dep = LockedDependency(repo_url="owner/repo", target_subset=["codex"])

        assert dep.to_dict()["target_subset"] == ["codex"]

    def test_targets_omitted_when_empty(self) -> None:
        dep = LockedDependency(repo_url="owner/repo")

        assert "target_subset" not in dep.to_dict()

    def test_from_dict_restores_targets(self) -> None:
        dep = LockedDependency.from_dict({"repo_url": "owner/repo", "target_subset": ["codex"]})

        assert dep.target_subset == ["codex"]

    def test_lockfile_round_trip(self) -> None:
        lock = LockFile()
        lock.add_dependency(LockedDependency(repo_url="owner/repo", target_subset=["codex"]))

        restored = LockFile.from_yaml(lock.to_yaml())

        assert restored.dependencies["owner/repo"].target_subset == ["codex"]
