"""Pure complete-source projections and typed replacement lock evidence."""

from copy import deepcopy
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile, LockfileFormatError
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import (
    ReplacementBinding,
    ReplacementConfigError,
    parse_replace_block,
    project_effective_reference,
    replacement_rule_to_dict,
)

pytestmark = pytest.mark.unit

_UPSTREAM = "https://github.com/owner/upstream"
_FORK = "https://github.com/fork/upstream"


def _binding(source: dict, *, ref: str = "v1.0") -> tuple[ReplacementBinding, DependencyReference]:
    original = DependencyReference(
        repo_url="owner/upstream",
        host="github.com",
        reference=ref,
        alias="chosen",
        skill_subset=["alpha"],
        target_subset=[],
    )
    rule = parse_replace_block([{"for": {"git": _UPSTREAM}, "with": source}])[0]
    binding = ReplacementBinding(original, rule)
    binding.record(original, None, False)
    effective = project_effective_reference(original, rule)
    effective.replacement = binding
    return binding, effective


@pytest.mark.parametrize("source", [{"path": "../local"}, {"git": _FORK, "ref": "fixed"}])
def test_source_projection_preserves_selection_without_mutating_original(source: dict) -> None:
    binding, effective = _binding(source)
    assert effective.alias == "chosen"
    assert effective.skill_subset == ["alpha"]
    assert effective.target_subset == []
    assert effective.ownership_reference is binding.original
    assert effective.get_unique_key() == "owner/upstream"
    assert effective.source_reference.replacement is None
    assert effective.source_reference.get_unique_key() != effective.get_unique_key()
    assert binding.original.reference == "v1.0"
    assert binding.original.replacement is None


@pytest.mark.parametrize("source", [{"path": "../local"}, {"git": _FORK, "ref": "fixed"}])
def test_source_projection_does_not_inherit_original_subpath(source: dict) -> None:
    original = DependencyReference.parse_from_dict({"git": _UPSTREAM, "path": "packages/base"})
    rule = parse_replace_block(
        [{"for": {"git": _UPSTREAM, "path": "packages/base"}, "with": source}]
    )[0]
    effective = project_effective_reference(original, rule)
    assert not effective.is_virtual
    assert effective.virtual_path is None
    assert original.virtual_path == "packages/base"


@pytest.mark.parametrize(
    ("git", "expected_scheme", "expected_user", "expected_insecure"),
    [
        ("https://github.com/fork/upstream", "https", None, False),
        ("ssh://git@github.com:2222/fork/upstream.git", "ssh", "git", False),
    ],
)
def test_source_projection_preserves_transport_without_granting_consent(
    git: str, expected_scheme: str, expected_user: str | None, expected_insecure: bool
) -> None:
    _, effective = _binding({"git": git, "ref": "fixed"})
    assert effective.explicit_scheme == expected_scheme
    assert effective.ssh_user == expected_user
    assert effective.is_insecure is expected_insecure
    assert not effective.allow_insecure
    assert effective.port == urlparse(git).port


def test_http_source_is_rejected_without_a_source_transport_consent_contract() -> None:
    with pytest.raises(ReplacementConfigError, match="HTTP replacement sources are not supported"):
        _binding({"git": "http://github.com/fork/upstream", "ref": "fixed"})


def test_source_projection_uses_effective_ado_coordinates() -> None:
    _, effective = _binding(
        {"git": "https://dev.azure.com/myorg/myproject/_git/myrepo", "ref": "fixed"}
    )
    effective.validate_provider_coordinates()
    assert (effective.ado_organization, effective.ado_project, effective.ado_repo) == (
        "myorg",
        "myproject",
        "myrepo",
    )


@pytest.mark.parametrize("source", [{"path": "../local"}, {"git": _FORK, "ref": "f" * 40}])
def test_original_lock_identity_with_nested_selected_source_evidence(source: dict) -> None:
    binding, effective = _binding(source)
    commit = None if effective.is_local else "f" * 40
    locked = LockedDependency.from_dependency_ref(effective, commit, 1, None)
    assert locked.get_unique_key() == "owner/upstream"
    assert locked.repo_url == "owner/upstream"
    assert locked.source is None
    assert locked.local_path is None
    assert locked.resolved_ref == effective.reference
    assert locked.to_dict()["replacement"] == {
        "source": source,
        "requests": [
            {
                "parent": None,
                "ref": "v1.0",
                "skills": ["alpha"],
                "targets": [],
                "dev": False,
                "alias": "chosen",
            }
        ],
    }
    lock = LockFile(replacements=[replacement_rule_to_dict(binding.rule)])
    lock.add_dependency(locked)
    restored = LockFile.from_yaml(lock.to_yaml())
    assert restored.lockfile_version == "3"
    selected = restored.get_dependency("owner/upstream").to_dependency_ref()
    assert selected.get_unique_key() == "owner/upstream"
    assert selected.repo_url == effective.repo_url
    assert selected.reference == effective.reference
    assert selected.skill_subset == ["alpha"]
    assert selected.target_subset == []
    assert selected.alias == "chosen"


def test_unreplaced_lock_entry_does_not_emit_evidence() -> None:
    locked = LockedDependency(repo_url="owner/upstream")
    assert "replacement" not in locked.to_dict()


@pytest.mark.parametrize("change", ["missing-table", "different-source", "missing-evidence"])
def test_lock_evidence_must_match_root_table(change: str) -> None:
    binding, effective = _binding({"path": "../local"})
    lock = LockFile(replacements=[replacement_rule_to_dict(binding.rule)])
    locked = LockedDependency.from_dependency_ref(effective, None, 1, None)
    lock.add_dependency(locked)
    if change == "missing-table":
        lock.replacements = None
    elif change == "different-source":
        lock.replacements[0]["with"] = {"path": "../other"}
    else:
        locked.replacement = None
    with pytest.raises(LockfileFormatError, match="disagrees"):
        lock.to_yaml()


@pytest.mark.parametrize(
    "field", ["source", "requests", "parent", "ref", "skills", "targets", "dev", "alias"]
)
def test_malformed_evidence_fails_closed(field: str) -> None:
    binding, _ = _binding({"path": "../local"})
    evidence = deepcopy(binding.to_dict())
    if field in {"source", "requests"}:
        evidence[field] = "invalid"
    else:
        evidence["requests"][0][field] = 42
    with pytest.raises(ReplacementConfigError):
        ReplacementBinding.from_dict(evidence, binding.original)


def test_local_evidence_rejects_invented_git_pin() -> None:
    _, effective = _binding({"path": "../local"})
    raw = LockedDependency.from_dependency_ref(effective, None, 1, None).to_dict()
    raw["resolved_commit"] = "a" * 40
    with pytest.raises(LockfileFormatError, match="must not carry"):
        LockedDependency.from_dict(raw)


def test_pin_reuse_distinguishes_changed_removed_and_same_source() -> None:
    _, effective = _binding({"git": _FORK, "ref": "main"})
    locked = LockedDependency.from_dependency_ref(effective, "a" * 40, 1, None)
    assert locked.matches_replacement(effective)
    _, other = _binding({"git": "https://github.com/other/upstream", "ref": "main"})
    assert not locked.matches_replacement(other)
    assert not locked.matches_replacement(effective.ownership_reference)
    upstream = LockedDependency(
        repo_url="owner/upstream", resolved_ref="main", resolved_commit="a" * 40
    )
    assert not upstream.matches_replacement(effective)
