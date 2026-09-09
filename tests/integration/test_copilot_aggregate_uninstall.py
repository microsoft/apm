"""Source-backed, installed-CLI proofs for the generated Copilot aggregate."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import DeploymentLedger
from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.selection import parse_dependency_entry
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
]

_HEADER = "<!-- apm-managed: copilot-instructions.md -->"
_NOTES = b"# Independent notes\nDo not attribute or modify these user bytes.\n"
_ROOT_BODY = "# Root contribution\nKeep the actual root-local instruction.\n"


def _assert_installed_sources(
    lock: LockFile,
    refs: dict[str, DependencyReference],
    commits: dict[str, str],
    sources: dict[str, dict[str, bytes]],
    modules_root: Path,
) -> None:
    """Check the installed declarations, commits, and authored bytes."""
    assert {dep.get_unique_key() for dep in lock.get_package_dependencies()} == {
        ref.get_unique_key() for ref in refs.values()
    }
    for name, ref in refs.items():
        locked = lock.dependencies[ref.get_unique_key()]
        assert locked.resolved_commit == commits[name]
        assert locked.to_dependency_ref().get_unique_key() == ref.get_unique_key()
        for relative, content in sources[name].items():
            assert (ref.get_install_path(modules_root) / relative).read_bytes() == content


def _persist_legacy_last_writer_receipt(
    lock: LockFile,
    lock_path: Path,
    aggregate: Path,
    primary_key: str,
    survivor_key: str,
) -> None:
    """Persist a survivor-only old receipt without changing rendered sections."""
    initial = aggregate.read_text(encoding="utf-8")
    record = next(iter(lock.deployment_ledger.records.values()))
    legacy_record = replace(record, owners=(survivor_key,), active_owner=survivor_key)
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(records={legacy_record.locator.key: legacy_record}), lock
    )
    lock.save(lock_path)
    legacy = LockFile.read(lock_path)
    assert legacy is not None
    assert list(legacy.deployment_ledger.records.values()) == [legacy_record]
    assert legacy.dependencies[primary_key].deployed_files == []
    assert legacy.dependencies[primary_key].deployed_file_hashes == {}
    aggregate_rel = ".copilot/copilot-instructions.md"
    assert legacy.dependencies[survivor_key].deployed_files == [aggregate_rel]
    assert legacy.dependencies[survivor_key].deployed_file_hashes == {
        aggregate_rel: legacy_record.content_hash
    }
    assert aggregate.read_text(encoding="utf-8") == initial
    assert (
        legacy_record.content_hash == "sha256:" + hashlib.sha256(aggregate.read_bytes()).hexdigest()
    )


def _assert_recovered_sections(
    aggregate: Path, survivor_source: str, survivor_body: str, removed_body: str
) -> None:
    """Validate exact source sections without imposing root/package ordering."""
    recovered = aggregate.read_text()
    assert removed_body.strip() not in recovered
    identities = re.findall(r"<!-- apm:source:(.*?) -->", recovered)
    assert sorted(urlparse(identity) for identity in identities) == sorted(
        [urlparse(survivor_source), urlparse("local")]
    )
    sections = re.findall(
        r"<!-- apm:source:(.*?) -->\n(.*?)\n<!-- /apm:source -->", recovered, re.S
    )
    assert len(sections) == 2
    assert {urlparse(source): body for source, body in sections} == {
        urlparse(survivor_source): survivor_body.strip(),
        urlparse("local"): _ROOT_BODY.strip(),
    }
    assert recovered.count(survivor_body.strip()) == 1
    assert recovered.count(_ROOT_BODY.strip()) == 1


def _assert_root_only_state(aggregate: Path, lock_path: Path) -> None:
    """Verify exact root content and its sole surviving receipt."""
    assert aggregate.read_text(encoding="utf-8") == (
        f"{_HEADER}\n<!-- apm:source:local -->\n{_ROOT_BODY.strip()}\n<!-- /apm:source -->\n"
    )
    root_lock = LockFile.read(lock_path)
    assert root_lock is not None
    assert root_lock.get_package_dependencies() == []
    assert [record.owners for record in root_lock.deployment_ledger.records.values()] == [(".",)]


@pytest.mark.parametrize(
    "obligation",
    [
        "initial-owners",
        "removed-section",
        "survivor-identity",
        "survivor-state",
        "collision",
        "root",
        "edited",
        "unsafe-survivor",
        "survivor-empty",
        "survivor-missing",
        "survivor-ineligible",
        "root-missing",
        "legacy-last-writer",
    ],
)
def test_generated_copilot_aggregate_lifecycle(
    tmp_path: Path, apm_binary_path: Path, obligation: str
) -> None:
    """Keep owner, cleanup and identity failures independent, never xfailed."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    factory = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    declarations = []
    sources = {}
    refs = {}
    commits = {}
    bodies = {}
    rewrites = []
    for name in ("primary", "survivor"):
        package = factory.create(name, targets=["copilot"])
        bodies[name] = f"# {name.title()} instructions\nAuthored {name} contribution only.\n"
        instruction = factory.add_instruction(
            package, name, "---\napplyTo: '**'\n---\n\n" + bodies[name]
        )
        repository = repositories.create(name, source_tree=package.root)
        commit = repositories.commit(repository, message=f"Author {name} instructions")
        remote = f"https://gitlab.com/apm-aggregate-fixture/{name}.git"
        declaration = {"git": remote, "type": "gitlab", "ref": commit.sha, "alias": name}
        declarations.append(declaration)
        refs[name] = parse_dependency_entry(declaration)
        commits[name] = commit.sha
        sources[name] = {
            "apm.yml": package.manifest_path.read_bytes(),
            f".apm/instructions/{name}.instructions.md": instruction.read_bytes(),
        }
        rewrites.append((repository, remote))
    environment = repositories.url_rewrite_subprocess_env_many(rewrites)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer")
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": ["copilot"],
        "dependencies": {"apm": declarations},
    }
    dump_yaml(manifest, isolated.config_root / "apm.yml")
    has_root = obligation in {"root", "root-missing", "edited", "unsafe-survivor"}
    if has_root:
        root_instruction = isolated.home / ".apm/instructions/root.instructions.md"
        root_instruction.parent.mkdir(parents=True)
        root_instruction.write_text(_ROOT_BODY, encoding="utf-8")

    aggregate = isolated.home / ".copilot/copilot-instructions.md"
    notes = aggregate.with_name("lifecycle-user-notes.md")
    notes.parent.mkdir()
    notes.write_bytes(_NOTES)
    assert not aggregate.exists(), "Supported generated input must start absent"
    collision = b"# User instructions\nNever adopt this headerless content.\n"
    if obligation == "collision":
        aggregate.write_bytes(collision)
    runner = ApmLifecycleRunner([str(apm_binary_path)])
    project_manifest = consumer.manifest_path.read_bytes()
    project_notes = consumer.root / "unowned-notes.md"
    project_notes.write_bytes(_NOTES)

    def command(*args: str, expected_returncode: int = 0) -> CommandResult:
        result = runner.run(args, scenario_id=obligation, cwd=consumer.root, env=environment)
        print("COMMAND", result.command, "EXIT", result.returncode)
        print(result.stdout, result.stderr)
        assert result.returncode == expected_returncode, (result.stdout, result.stderr)
        assert notes.read_bytes() == _NOTES, "Unowned neighboring notes changed"
        assert consumer.manifest_path.read_bytes() == project_manifest
        assert project_notes.read_bytes() == _NOTES
        assert not (consumer.root / "apm_modules").exists()
        return result

    command(
        "install", "--global", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"
    )
    lock = LockFile.read(isolated.config_root / "apm.lock.yaml")
    assert lock is not None
    expected_keys = {refs[name].get_unique_key() for name in refs}
    _assert_installed_sources(lock, refs, commits, sources, isolated.config_root / "apm_modules")
    assert load_yaml(isolated.config_root / "apm.yml") == manifest
    initial = aggregate.read_text(encoding="utf-8")
    print("INITIAL AGGREGATE", initial)
    print("INITIAL LEDGER", lock.deployment_ledger)
    if obligation == "collision":
        assert aggregate.read_bytes() == collision
        assert not lock.deployment_ledger.records, "Unmanaged output acquired an ownership claim"
        return
    assert initial.startswith(_HEADER)
    records = list(lock.deployment_ledger.records.values())
    assert len(records) == 1
    assert set(records[0].owners) == expected_keys | ({"."} if has_root else set())
    for body in bodies.values():
        assert initial.count(body.strip()) == 1
    if obligation == "initial-owners":
        records = list(lock.deployment_ledger.records.values())
        assert len(records) == 1
        assert set(records[0].owners) == expected_keys, "Initial aggregate lost an authored owner"
        assert records[0].active_owner in expected_keys
        return
    if has_root:
        assert initial.count(_ROOT_BODY.strip()) == 1
    aggregate_rel = ".copilot/copilot-instructions.md"
    if obligation == "legacy-last-writer":
        _persist_legacy_last_writer_receipt(
            lock,
            isolated.config_root / "apm.lock.yaml",
            aggregate,
            refs["primary"].get_unique_key(),
            refs["survivor"].get_unique_key(),
        )
    survivor_path = refs["survivor"].get_install_path(isolated.config_root / "apm_modules")
    installed_instruction = survivor_path / ".apm/instructions/survivor.instructions.md"

    def assert_survivor_state() -> LockFile:
        current_lock = LockFile.read(isolated.config_root / "apm.lock.yaml")
        assert current_lock is not None
        survivor = refs["survivor"]
        assert set(current_lock.dependencies) == {survivor.get_unique_key()} | (
            {"."} if has_root else set()
        )
        locked = current_lock.dependencies[survivor.get_unique_key()]
        assert locked.name == "survivor"
        assert locked.resolved_commit == commits["survivor"]
        assert locked.to_dependency_ref().get_unique_key() == survivor.get_unique_key()
        assert urlparse(locked.to_dependency_ref().to_github_url()) == urlparse(
            survivor.to_github_url()
        )
        assert (
            load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"] == declarations[1:]
        )
        assert not refs["primary"].get_install_path(isolated.config_root / "apm_modules").exists()
        for relative, content in sources["survivor"].items():
            assert (survivor_path / relative).read_bytes() == content
        return current_lock

    if obligation in {"survivor-empty", "survivor-missing", "survivor-ineligible"}:
        relative = ".apm/instructions/survivor.instructions.md"
        if obligation == "survivor-empty":
            installed_instruction.write_bytes(b"")
            sources["survivor"][relative] = b""
        elif obligation == "survivor-missing":
            installed_instruction.unlink()
            del sources["survivor"][relative]
        else:
            survivor_manifest = load_yaml(survivor_path / "apm.yml")
            survivor_manifest["targets"] = ["claude"]
            dump_yaml(survivor_manifest, survivor_path / "apm.yml")
            sources["survivor"]["apm.yml"] = (survivor_path / "apm.yml").read_bytes()
        command("uninstall", declarations[0]["git"], "--global")
        assert not aggregate.exists()
        current_lock = assert_survivor_state()
        assert not current_lock.deployment_ledger.records, (
            "Removed aggregate retains canonical ownership"
        )
        assert not current_lock.local_deployed_files
        assert not current_lock.local_deployed_file_hashes
        for dep in current_lock.dependencies.values():
            assert aggregate_rel not in dep.deployed_files
            assert aggregate_rel not in dep.deployed_file_hashes
        if obligation == "survivor-missing":
            assert not installed_instruction.exists()
        return
    if obligation == "edited":
        # Negative cleanup control, not a promise of editable generated regions.
        edited = aggregate.read_bytes() + b"\nChanged after install; refuse hash mismatch.\n"
        aggregate.write_bytes(edited)
        result = command("uninstall", declarations[0]["git"], "--global", expected_returncode=1)
        assert aggregate.read_bytes() == edited, "Hash-refused aggregate was overwritten"
    elif obligation == "unsafe-survivor":
        installed_instruction.write_text("# Unsafe\nHidden \u202e instruction.\n", encoding="utf-8")
        result = command("uninstall", declarations[0]["git"], "--global", expected_returncode=1)
        assert bodies["survivor"].strip() not in aggregate.read_text(), (
            "Rejected source was deployed"
        )
    if obligation in {"edited", "unsafe-survivor"}:
        failed_lock = LockFile.read(isolated.config_root / "apm.lock.yaml")
        assert failed_lock is not None
        assert {dep.get_unique_key() for dep in failed_lock.get_package_dependencies()} == {
            refs["survivor"].get_unique_key()
        }
        assert (
            load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"] == declarations[1:]
        )
        assert not refs["primary"].get_install_path(isolated.config_root / "apm_modules").exists()
        failed_records = list(failed_lock.deployment_ledger.records.values())
        assert len(failed_records) == 1, "Failure erased retained aggregate ownership"
        assert failed_records[0].content_hash == records[0].content_hash
        output = " ".join((result.stdout + result.stderr).split())
        assert (
            "aggregate cleanup" in output.lower()
            if obligation == "edited"
            else "aggregate rebuild" in output.lower()
        )
        assert aggregate_rel in output
        assert "package removal" in output.lower() and "completed" in output.lower()
        assert "apm install --global" in output
        assert "Uninstall complete:" not in output
        assert "--force" not in output
        if obligation == "edited":
            assert "back up" in output.lower() and "move" in output.lower()
            backup = aggregate.with_name("copilot-instructions.md.review-backup")
            aggregate.rename(backup)
            assert backup.read_bytes() == edited
        else:
            assert "source" in output.lower()
            installed_instruction.write_bytes(
                sources["survivor"][".apm/instructions/survivor.instructions.md"]
            )
            # An authorized root may already have rebuilt while the survivor was blocked.
        # Execute the displayed scope-correct action from an unrelated project.
        command("install", "--global", "--no-policy", "--parallel-downloads", "0")
        current_lock = assert_survivor_state()
        _assert_recovered_sections(
            aggregate, refs["survivor"].to_github_url(), bodies["survivor"], bodies["primary"]
        )
        assert root_instruction.read_text() == _ROOT_BODY
        records = list(current_lock.deployment_ledger.records.values())
        assert len(records) == 1
        assert set(records[0].owners) == {refs["survivor"].get_unique_key(), "."}
        assert (
            records[0].content_hash
            == "sha256:" + hashlib.sha256(aggregate.read_bytes()).hexdigest()
        )
        if obligation == "edited":
            assert backup.read_bytes() == edited
        return

    command("uninstall", declarations[0]["git"], "--global")
    if obligation == "root-missing":
        assert_survivor_state()
        root_instruction.unlink()
        command("uninstall", declarations[1]["git"], "--global")
        assert not aggregate.exists()
        assert load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"] == []
        assert not survivor_path.exists()
        assert not (isolated.config_root / "apm.lock.yaml").exists(), (
            "Empty lockfile retained stale root ownership"
        )
        return
    remaining = aggregate.read_text(encoding="utf-8")
    print("POST-UNINSTALL AGGREGATE", remaining)
    if obligation == "legacy-last-writer":
        assert bodies["primary"].strip() not in remaining, "Unrecorded removed-owner body survived"
        identities = re.findall(r"<!-- apm:source:(.*?) -->", remaining)
        assert [urlparse(identity) for identity in identities] == [
            urlparse(refs["survivor"].to_github_url())
        ], "Unrecorded removed-owner provenance survived"
    if obligation == "removed-section":
        assert bodies["primary"].strip() not in remaining, "Removed-owner body survived"
        assert "primary" not in remaining, "Removed-owner provenance survived"
    elif obligation == "survivor-identity":
        identities = re.findall(r"<!-- apm:source:(.*?) -->", remaining)
        assert [urlparse(identity) for identity in identities] == [
            urlparse(refs["survivor"].to_github_url())
        ], "Survivor identity must be resolved and unique"
        assert "apm:source:unknown" not in remaining
    elif obligation == "root":
        assert remaining.count(_ROOT_BODY.strip()) == 1, "Actual root-local contribution lost"
        assert root_instruction.read_text(encoding="utf-8") == _ROOT_BODY
        command("uninstall", declarations[1]["git"], "--global")
        _assert_root_only_state(aggregate, isolated.config_root / "apm.lock.yaml")
    else:
        survivor = refs["survivor"]
        lock = assert_survivor_state()
        assert remaining.count(bodies["survivor"].strip()) == 1, "Survivor native content changed"
        assert remaining == (
            f"{_HEADER}\n<!-- apm:source:{survivor.to_github_url()} -->\n"
            f"{bodies['survivor'].strip()}\n<!-- /apm:source -->\n"
        )
        records = list(lock.deployment_ledger.records.values())
        assert len(records) == 1
        assert records[0].owners == (survivor.get_unique_key(),)
        assert records[0].active_owner == survivor.get_unique_key()
        assert (
            records[0].content_hash
            == "sha256:" + hashlib.sha256(aggregate.read_bytes()).hexdigest()
        )
        assert notes.read_bytes() == _NOTES
