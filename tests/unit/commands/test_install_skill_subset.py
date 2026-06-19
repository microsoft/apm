"""Regression test for #1395: --skill filter must persist to apm.yml."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_apm_yml(path: Path, deps: list | None = None) -> Path:
    """Write a minimal apm.yml."""
    import yaml

    data = {"name": "test", "version": "1.0.0"}
    if deps is not None:
        data["dependencies"] = {"apm": deps}
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def _run_validate(apm_yml: Path, skill_subset=None) -> MagicMock:
    """Call _validate_and_add_packages_to_apm_yml with a mocked dep_ref.

    Returns the mock dep_ref so callers can inspect dep_ref.skill_subset.
    """
    from apm_cli.commands.install import _validate_and_add_packages_to_apm_yml

    mock_logger = MagicMock()
    mock_logger.verbose = False
    mock_logger.validation_summary.return_value = True

    mock_dep_ref = MagicMock()
    mock_dep_ref.to_canonical.return_value = "github.com/org/repo"
    mock_dep_ref.get_identity.return_value = "org/repo"
    mock_dep_ref.is_insecure = False
    mock_dep_ref.is_virtual = False
    mock_dep_ref.is_local = False
    mock_dep_ref.local_path = None
    mock_dep_ref.reference = None
    mock_dep_ref.alias = None
    mock_dep_ref.host = "github.com"
    mock_dep_ref.skill_subset = None

    def fake_to_apm_yml_entry():
        if mock_dep_ref.skill_subset:
            return {"git": "org/repo", "skills": list(mock_dep_ref.skill_subset)}
        return "org/repo"

    mock_dep_ref.to_apm_yml_entry = fake_to_apm_yml_entry

    with (
        patch(
            "apm_cli.commands.install._validate_package_exists",
            return_value=True,
        ),
        patch(
            "apm_cli.commands.install.resolve_parsed_dependency_reference",
            return_value=(mock_dep_ref, False),
        ),
        patch(
            "apm_cli.commands.install.user_scope_rejection_reason",
            return_value=None,
        ),
    ):
        _validate_and_add_packages_to_apm_yml(
            ["org/repo"],
            dry_run=False,
            logger=mock_logger,
            manifest_path=apm_yml,
            skill_subset=skill_subset,
        )

    return mock_dep_ref


def test_skill_subset_persisted_to_apm_yml(tmp_path):
    """When --skill is passed, apm.yml entry must include skills: list."""
    apm_yml = tmp_path / "apm.yml"
    _make_apm_yml(apm_yml)

    _run_validate(apm_yml, skill_subset=("skill-a", "skill-b"))

    import yaml

    data = yaml.safe_load(apm_yml.read_text(encoding="utf-8"))
    apm_deps = data["dependencies"]["apm"]
    assert len(apm_deps) == 1
    entry = apm_deps[0]
    assert isinstance(entry, dict), f"Expected dict entry with skills:, got {type(entry)}: {entry}"
    assert "skills" in entry
    assert sorted(entry["skills"]) == ["skill-a", "skill-b"]


def test_skill_subset_not_set_when_absent(tmp_path):
    """When --skill is NOT passed, apm.yml entry should be a plain string."""
    apm_yml = tmp_path / "apm.yml"
    _make_apm_yml(apm_yml)

    _run_validate(apm_yml)

    import yaml

    data = yaml.safe_load(apm_yml.read_text(encoding="utf-8"))
    apm_deps = data["dependencies"]["apm"]
    assert len(apm_deps) == 1
    assert isinstance(apm_deps[0], str), "Without --skill, entry should be a plain string"


# ---------------------------------------------------------------------------
# Normalization tests (Finding 2 from PR #1442 review)
# ---------------------------------------------------------------------------


def test_skill_subset_strips_whitespace(tmp_path):
    """Skill names with surrounding whitespace are stripped before persistence."""
    apm_yml = tmp_path / "apm.yml"
    _make_apm_yml(apm_yml)

    dep_ref = _run_validate(apm_yml, skill_subset=("  skill-a  ", "\tskill-b\t"))

    assert dep_ref.skill_subset == ["skill-a", "skill-b"]


def test_skill_subset_rejects_empty_strings(tmp_path):
    """Empty strings (or whitespace-only) are dropped before persistence."""
    apm_yml = tmp_path / "apm.yml"
    _make_apm_yml(apm_yml)

    dep_ref = _run_validate(apm_yml, skill_subset=("skill-a", "", "   ", "skill-b"))

    assert dep_ref.skill_subset == ["skill-a", "skill-b"]


def test_skill_subset_deduplicates_preserving_order(tmp_path):
    """Duplicate skill names are removed; result is sorted."""
    apm_yml = tmp_path / "apm.yml"
    _make_apm_yml(apm_yml)

    dep_ref = _run_validate(
        apm_yml, skill_subset=("skill-b", "skill-a", "skill-b", "skill-a", "skill-c")
    )

    assert dep_ref.skill_subset == ["skill-a", "skill-b", "skill-c"]
