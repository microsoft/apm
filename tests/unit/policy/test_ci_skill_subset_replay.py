"""Subset audit must inspect prepared dependencies, not incidental checkout state."""

from pathlib import Path

import pytest

from apm_cli.install.audit_replay import PreparedCiAuditReplay
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.policy.ci_checks import run_baseline_checks
from apm_cli.utils.yaml_io import dump_yaml, load_yaml

pytestmark = pytest.mark.component


@pytest.fixture(params=["dependencies", "devDependencies"])
def subset_project(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Write matching manifest and lock selections for a repository subdirectory."""
    project = tmp_path / "checkout"
    project.mkdir()
    dump_yaml(
        {
            "name": "subset-audit",
            "version": "1.0.0",
            "targets": ["copilot"],
            request.param: {
                "apm": [
                    {
                        "git": "owner/repo",
                        "path": "plugins/tools",
                        "ref": "v1",
                        "skills": ["alpha"],
                    }
                ]
            },
        },
        project / "apm.yml",
    )
    dump_yaml(
        {
            "lockfile_version": "1",
            "dependencies": [
                {
                    "repo_url": "owner/repo",
                    "virtual_path": "plugins/tools",
                    "is_virtual": True,
                    "resolved_ref": "v1",
                    "package_type": "skill_bundle",
                    "skill_subset": ["alpha"],
                }
            ],
        },
        project / "apm.lock.yaml",
    )
    return project


def _write_skills(modules_root: Path, names: tuple[str, ...]) -> None:
    """Populate a real dependency install path without mocking path resolution."""
    ref = DependencyReference.parse_from_dict({"git": "owner/repo", "path": "plugins/tools"})
    package = ref.get_install_path(modules_root)
    package.mkdir(parents=True)
    for name in names:
        skill = package / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: Test skill\n---\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("checkout_skills", "replay_skills", "passed"),
    [
        (None, ("alpha",), True),
        (("alpha",), ("alpha",), True),
        (("stale",), ("alpha",), True),
        (("alpha",), ("beta",), False),
        (None, (), False),
    ],
    ids=["absent", "present", "stale", "invalid-despite-checkout", "invalid-absent"],
)
def test_baseline_subset_uses_prepared_tree(
    subset_project: Path,
    tmp_path: Path,
    checkout_skills: tuple[str, ...] | None,
    replay_skills: tuple[str, ...],
    passed: bool,
) -> None:
    """Prepared package contents take precedence in both pass and failure cases."""
    if checkout_skills is not None:
        _write_skills(subset_project / "apm_modules", checkout_skills)
    scratch = tmp_path / "scratch"
    modules = scratch / "apm_modules"
    _write_skills(modules, replay_skills)
    prepared = PreparedCiAuditReplay(
        scratch_root=scratch,
        modules_root=modules,
        lockfile_path=subset_project / "apm.lock.yaml",
        tracked_files=None,
        targets=(),
    )
    before = {path: path.read_bytes() for path in subset_project.rglob("*") if path.is_file()}

    result = run_baseline_checks(subset_project, fail_fast=False, prepared_replay=prepared)

    check = next(check for check in result.checks if check.name == "skill-subset-consistency")
    assert next(check for check in result.checks if check.name == "ref-consistency").passed
    assert check.passed is passed, check.details
    assert check.details == (
        []
        if passed
        else [
            "owner/repo/plugins/tools: recorded skill subset path(s) "
            "not found in package tree: alpha"
        ]
    )
    assert {
        path: path.read_bytes() for path in subset_project.rglob("*") if path.is_file()
    } == before
    assert (subset_project / "apm_modules").exists() is (checkout_skills is not None)


@pytest.mark.parametrize("checkout_skills", [None, ("alpha",), ("stale",)])
def test_subset_without_prepared_replay_checks_checkout(
    subset_project: Path, checkout_skills: tuple[str, ...] | None
) -> None:
    """Callers without a prepared replay retain real local subset validation."""
    if checkout_skills is not None:
        _write_skills(subset_project / "apm_modules", checkout_skills)

    result = run_baseline_checks(subset_project, fail_fast=False)

    check = next(check for check in result.checks if check.name == "skill-subset-consistency")
    assert check.passed is (checkout_skills == ("alpha",))


@pytest.mark.parametrize("checkout_skills", [None, ("alpha",)])
def test_subset_fails_closed_on_prepared_replay_error(
    subset_project: Path, checkout_skills: tuple[str, ...] | None
) -> None:
    """A failed replay preparation must fail the check, not fall back to checkout state."""
    if checkout_skills is not None:
        _write_skills(subset_project / "apm_modules", checkout_skills)

    result = run_baseline_checks(
        subset_project,
        fail_fast=False,
        prepared_replay_error="scratch materialization failed: disk quota exceeded",
    )

    for check_name in ("skill-subset-consistency", "config-consistency"):
        check = next(check for check in result.checks if check.name == check_name)
        assert check.passed is False
        assert check.details == ["scratch materialization failed: disk quota exceeded"]
        assert check.message == "replay failed: scratch materialization failed: disk quota exceeded"


def test_prepared_tree_does_not_override_manifest_lock_subset_mismatch(
    subset_project: Path, tmp_path: Path
) -> None:
    """Even a tree containing both selections cannot hide a manifest/lock mismatch."""
    lock_path = subset_project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    lock["dependencies"][0]["skill_subset"] = ["beta"]
    dump_yaml(lock, lock_path)
    scratch = tmp_path / "scratch"
    modules = scratch / "apm_modules"
    _write_skills(modules, ("alpha", "beta"))
    prepared = PreparedCiAuditReplay(
        scratch_root=scratch,
        modules_root=modules,
        lockfile_path=lock_path,
        tracked_files=None,
        targets=(),
    )

    result = run_baseline_checks(subset_project, fail_fast=False, prepared_replay=prepared)

    check = next(check for check in result.checks if check.name == "skill-subset-consistency")
    assert check.passed is False
    assert check.details == [
        "owner/repo/plugins/tools: manifest skills ['alpha'] != lockfile skill_subset ['beta']"
    ]
