"""Installed context identity and exact-byte snapshot regression tests."""

import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.contracts.frontend import parse_contract, plan_contract
from apm_cli.contracts.imports import read_project_manifest, resolve_installed_skills
from apm_cli.contracts.models import ContractError, ContractLimits
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = pytest.mark.component

SKILL = b"---\nname: handoff-style\ndescription: A self-contained style guide.\n---\nInclude a distinctive marker.\n"


def fixture(tmp_path: Path, *, local: bool = True) -> tuple[Path, Path, LockFile]:
    entry = (
        "    - path: ../handoff-style"
        if local
        else "    - git: https://github.com/fixtures/handoff-style.git\n      ref: v1"
    )
    (tmp_path / "apm.yml").write_text(
        "name: fixture\nversion: 1.0.0\ndependencies:\n  apm:\n" + entry + "\n",
        encoding="utf-8",
    )
    dependency = (
        DependencyReference.parse_from_dict({"path": "../handoff-style"})
        if local
        else DependencyReference.parse_from_dict(
            {"git": "https://github.com/fixtures/handoff-style.git", "ref": "v1"}
        )
    )
    installed = dependency.get_install_path(tmp_path / "apm_modules")
    installed.mkdir(parents=True)
    (installed / "apm.yml").write_text("name: handoff-style\nversion: 1.0.0\n", encoding="utf-8")
    (installed / "SKILL.md").write_bytes(SKILL)
    locked = LockedDependency(
        repo_url="_local/handoff-style" if local else dependency.repo_url,
        host=dependency.host,
        local_path="../handoff-style" if local else None,
        source="local" if local else None,
        resolved_ref=None if local else "v1",
        resolved_commit=None if local else "a" * 40,
        content_hash=None if local else compute_package_hash(installed),
        package_type="skill",
    )
    lock = LockFile()
    lock.add_dependency(locked)
    (tmp_path / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    source = tmp_path / "work.contract.md"
    source.write_text(
        "---\nproduces: result\nimports: [handoff-style]\nverify: {ok: 'true'}\n---\nWork.\n",
        encoding="utf-8",
    )
    return source, installed, lock


def resolve(source: Path):
    package, _, _ = read_project_manifest(source.parent, ContractLimits())
    return resolve_installed_skills(parse_contract(source), source.parent, package)


def test_local_identity_and_snapshot_do_not_claim_locked_content_hash(tmp_path: Path) -> None:
    source, installed, lock = fixture(tmp_path)
    skills, digest = resolve(source)
    assert digest == hashlib.sha256((tmp_path / "apm.lock.yaml").read_bytes()).hexdigest()
    assert len(skills) == 1
    assert skills[0].name == "handoff-style"
    assert skills[0].source_path == installed / "SKILL.md"
    assert skills[0].source_digest == hashlib.sha256(SKILL).hexdigest()
    assert skills[0].content == SKILL.decode()
    assert skills[0].lock_identity == next(iter(lock.dependencies))
    assert skills[0].verified_package_hash is None
    assert skills[0].assurance == "observed-local-source"
    (installed / "SKILL.md").write_bytes(SKILL.replace(b"\n", b"\r\n"))
    assert resolve(source)[0][0] != skills[0]


def test_git_import_requires_current_hash_and_ref(tmp_path: Path) -> None:
    source, installed, lock = fixture(tmp_path, local=False)
    skills, _ = resolve(source)
    assert skills[0].verified_package_hash == compute_package_hash(installed)
    assert skills[0].resolved_commit == "a" * 40
    (installed / "SKILL.md").write_bytes(SKILL + b"changed")
    with pytest.raises(ContractError, match="hash"):
        resolve(source)
    (installed / "SKILL.md").write_bytes(SKILL)
    next(iter(lock.dependencies.values())).resolved_ref = "v0"
    (tmp_path / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    with pytest.raises(ContractError, match="reference"):
        resolve(source)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_lock",
        "malformed_lock",
        "missing_install",
        "extra_resource",
        "nested_symlink",
        "closure",
    ],
)
def test_import_refusals_never_install(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, installed, _ = fixture(tmp_path)
    forbidden = Mock(side_effect=AssertionError("unexpected process/install"))
    monkeypatch.setattr("subprocess.run", forbidden)
    if failure == "missing_lock":
        (tmp_path / "apm.lock.yaml").unlink()
    elif failure == "malformed_lock":
        (tmp_path / "apm.lock.yaml").write_text("not: [yaml", encoding="utf-8")
    elif failure == "missing_install":
        (installed / "SKILL.md").unlink()
    elif failure == "extra_resource":
        (installed / "helper.py").write_text("print('not imported')", encoding="utf-8")
    elif failure == "nested_symlink":
        (installed / "helper").symlink_to(installed / "SKILL.md")
    elif failure == "closure":
        (installed / "apm.yml").write_text(
            "name: handoff-style\nversion: 1.0.0\ndependencies:\n  apm: [fixtures/another]\n",
            encoding="utf-8",
        )
    with pytest.raises(ContractError):
        resolve(source)
    forbidden.assert_not_called()


def test_missing_git_hash_is_not_fabricated(tmp_path: Path) -> None:
    source, _, lock = fixture(tmp_path, local=False)
    next(iter(lock.dependencies.values())).content_hash = None
    (tmp_path / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    with pytest.raises(ContractError, match="package hash"):
        resolve(source)


def test_legacy_lock_read_does_not_migrate(tmp_path: Path) -> None:
    source, _, _ = fixture(tmp_path)
    (tmp_path / "apm.lock.yaml").rename(tmp_path / "apm.lock")
    skills, digest = resolve(source)
    assert skills and digest
    assert (tmp_path / "apm.lock").is_file()
    assert not (tmp_path / "apm.lock.yaml").exists()


@pytest.mark.windows_compat
def test_bom_manifest_and_lock_are_parsed_by_yaml_owner(tmp_path: Path) -> None:
    source, _, _ = fixture(tmp_path)
    for name in ("apm.yml", "apm.lock.yaml"):
        path = tmp_path / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    skills, digest = resolve(source)
    assert skills[0].name == "handoff-style"
    assert digest == hashlib.sha256((tmp_path / "apm.lock.yaml").read_bytes()).hexdigest()


def test_duplicate_skill_name_refuses_ambiguity(tmp_path: Path) -> None:
    source, _, lock = fixture(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: fixture\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - path: ../handoff-style\n    - path: ../another\n",
        encoding="utf-8",
    )
    other = DependencyReference.parse_from_dict({"path": "../another"}).get_install_path(
        tmp_path / "apm_modules"
    )
    other.mkdir(parents=True)
    (other / "SKILL.md").write_bytes(SKILL)
    lock.add_dependency(
        LockedDependency(repo_url="_local/another", source="local", local_path="../another")
    )
    (tmp_path / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    with pytest.raises(ContractError) as error:
        resolve(source)
    assert error.value.code == "ambiguous_import"


def test_full_dependency_identity_and_virtual_subdirectory_are_supported(tmp_path: Path) -> None:
    source, installed, lock = fixture(tmp_path, local=False)
    declaration = {
        "git": "https://github.com/fixtures/handoff-style.git",
        "path": "skills/style",
        "ref": "v1",
    }
    dependency = DependencyReference.parse_from_dict(declaration)
    new_path = dependency.get_install_path(tmp_path / "apm_modules")
    new_path.mkdir(parents=True, exist_ok=True)
    for name in ("apm.yml", "SKILL.md"):
        (installed / name).rename(new_path / name)
    (tmp_path / "apm.yml").write_text(
        "name: fixture\nversion: 1.0.0\ndependencies:\n  apm:\n"
        "    - git: https://github.com/fixtures/handoff-style.git\n"
        "      path: skills/style\n      ref: v1\n",
        encoding="utf-8",
    )
    old = next(iter(lock.dependencies.values()))
    old.is_virtual = True
    old.virtual_path = "skills/style"
    lock.dependencies.clear()
    lock.add_dependency(old)
    (tmp_path / "apm.lock.yaml").write_text(lock.to_yaml(), encoding="utf-8")
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "imports: [handoff-style]", "imports: [fixtures/handoff-style/skills/style]"
        ),
        encoding="utf-8",
    )
    skills, _ = resolve(source)
    assert skills[0].source_path == new_path / "SKILL.md"


def test_replanning_changes_on_source_manifest_or_lock_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, installed, _ = fixture(tmp_path)
    binary = tmp_path / "native"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("apm_cli.runtime.utils.find_runtime_binary", lambda name: str(binary))
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.setattr("apm_cli.contracts.frontend.sys.platform", "linux")
    first = plan_contract(source, tmp_path, harness="copilot")
    (installed / "SKILL.md").write_bytes(b"\xef\xbb\xbf" + SKILL)
    second = plan_contract(source, tmp_path, harness="copilot")
    assert first != second
    assert second.imported_skills[0].content.encode("utf-8") == b"\xef\xbb\xbf" + SKILL
    manifest = tmp_path / "apm.yml"
    manifest.write_bytes(manifest.read_bytes() + b"# changed\n")
    third = plan_contract(source, tmp_path, harness="copilot")
    assert third.manifest_digest != second.manifest_digest
    lock = tmp_path / "apm.lock.yaml"
    lock.write_bytes(lock.read_bytes() + b"# changed\n")
    fourth = plan_contract(source, tmp_path, harness="copilot")
    assert third.lock_digest != fourth.lock_digest
