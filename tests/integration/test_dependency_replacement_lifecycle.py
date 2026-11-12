"""Root source replacement contracts through an installed APM executable."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.copilot_plugins.registrar import catalog_path_for
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import parse_replace_block, replacement_rule_to_dict
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_unmanaged_skill_preflight_lifecycle import _native_bytes
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)
_UPSTREAM = "https://github.com/apm-replacement-fixture/base"
_FORK = "https://github.com/apm-replacement-fixture/fork"
_INSTRUCTION = ".github/instructions/base.instructions.md"
_INSTALL = ("install", "--no-policy", "--target", "copilot", "--parallel-downloads", "0")


def _content(marker: str) -> str:
    return f"---\napplyTo: '**'\n---\n# {marker}\n"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for prefix in ("apm.lock.yaml", ".github", "apm_modules")
        for path in ([root / prefix] if (root / prefix).is_file() else (root / prefix).rglob("*"))
        if path.is_file()
    }


@dataclass(frozen=True)
class ReplacementFixture:
    consumer: LocalPackage
    local: LocalPackage
    work_factory: LocalPackageFactory
    upstream_sha: str
    fork_sha: str
    upstream: LocalGitRepository
    fork: LocalGitRepository
    repositories: LocalGitRepositoryFactory
    environment: dict[str, str]
    runner: ApmLifecycleRunner

    def run(self, *args: str, root: Path | None = None) -> CommandResult:
        return self.runner.run(
            args or _INSTALL,
            cwd=root or self.consumer.root,
            env=self.environment,
            scenario_id="root-source-replacement",
        )

    def install(self) -> None:
        result = self.run()
        assert result.returncode == 0, result.stdout + result.stderr

    def replace(self, source: dict[str, str] | None) -> None:
        manifest = load_yaml(self.consumer.manifest_path)
        if source is None:
            manifest.pop("replace", None)
            manifest.pop("$schema", None)
        else:
            manifest["$schema"] = _SCHEMA
            manifest["replace"] = [{"for": {"git": _UPSTREAM}, "with": source}]
        dump_yaml(manifest, self.consumer.manifest_path)

    def assert_bytes(self, marker: str, *, root: Path | None = None) -> None:
        project = root or self.consumer.root
        assert (project / _INSTRUCTION).read_bytes() == _content(marker).encode("utf-8")

    def locked_entry(self) -> dict:
        locked = load_yaml(self.consumer.root / "apm.lock.yaml")
        assert len(locked["dependencies"]) == 1
        entry = locked["dependencies"][0]
        assert entry["repo_url"] == "apm-replacement-fixture/base"
        assert not entry.get("local_path")
        modules = self.consumer.root / "apm_modules"
        manifests = sorted(
            path.relative_to(modules).as_posix() for path in modules.rglob("apm.yml")
        )
        assert manifests == ["apm-replacement-fixture/base/apm.yml"]
        return entry


def _replacement_fixture(tmp_path: Path, apm_binary_path: Path) -> ReplacementFixture:
    isolated = IsolatedApmEnvironment.create(tmp_path / "replacement", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    source_factory = LocalPackageFactory(isolated.package_root)
    upstream = source_factory.create("base", targets=("copilot",))
    source_factory.add_instruction(upstream, "base", _content("UPSTREAM"))
    fork_source = source_factory.create("fork", targets=("copilot",))
    source_factory.add_instruction(fork_source, "base", _content("FORK"))
    work_factory = LocalPackageFactory(isolated.work_root)
    local = work_factory.create("local-base", targets=("copilot",))
    work_factory.add_instruction(local, "base", _content("LOCAL"))
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    origin = repositories.create("base", source_tree=upstream.root)
    upstream_sha = repositories.commit(origin, message="Original source").sha
    fork = repositories.create("fork", source_tree=fork_source.root)
    fork_sha = repositories.commit(fork, message="Replacement source").sha
    environment = repositories.url_rewrite_subprocess_env_many(((origin, _UPSTREAM), (fork, _FORK)))
    environment["APM_POLICY_DISABLE"] = "1"
    consumer = work_factory.create(
        "consumer",
        dependencies=({"git": _UPSTREAM, "ref": upstream_sha},),
        targets=("copilot",),
    )
    return ReplacementFixture(
        consumer,
        local,
        work_factory,
        upstream_sha,
        fork_sha,
        origin,
        fork,
        repositories,
        environment,
        ApmLifecycleRunner((str(apm_binary_path),)),
    )


def _assert_empty_graph_replacement_lifecycle(
    tmp_path: Path, runner: ApmLifecycleRunner, keep_targets: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "empty-graph", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    dependency = packages.create("local-skill")
    packages.add_skill(
        dependency, "demo", "---\nname: demo\ndescription: Fixture\n---\n\nFixture.\n"
    )
    consumers = LocalPackageFactory(isolated.work_root)
    consumer = consumers.create(
        "consumer", dependencies=({"path": str(dependency.root)},), targets=("claude", "codex")
    )
    manifest = load_yaml(consumer.manifest_path)
    manifest.update({"$schema": _SCHEMA, "replace": []})
    lock_path = consumer.root / "apm.lock.yaml"

    def install(*flags: str, success: bool = True) -> CommandResult:
        dump_yaml(manifest, consumer.manifest_path)
        manifest_bytes = consumer.manifest_path.read_bytes()
        result = runner.run(("install", "--no-policy", *flags), cwd=consumer.root, env=environment)
        assert (result.returncode == 0) is success, result.stdout + result.stderr
        assert consumer.manifest_path.read_bytes() == manifest_bytes
        return result

    install()
    assert load_yaml(lock_path)["dependencies"]
    unmanaged = consumer.root / ".agents/skills/user/SKILL.md"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_bytes(b"Unmanaged content\n")
    manifest["dependencies"] = {"apm": []}
    install()
    empty = load_yaml(lock_path)
    assert empty["dependencies"] == []
    assert empty["deployments"] == []
    assert empty["replacements"] == []
    if not keep_targets:
        manifest.pop("targets", None)

    rule = {
        "for": {"git": "https://github.com/unused-replacement-fixture/base.git"},
        "with": {"path": "../intentionally-absent"},
    }
    for table in ([rule], [], [rule], None):
        if table is None:
            manifest.pop("replace")
        else:
            manifest["replace"] = table
        before = _snapshot(consumer.root)
        install("--dry-run")
        assert _snapshot(consumer.root) == before
        refused = install("--frozen", success=False)
        assert "replacement rules differ" in refused.stdout + refused.stderr
        assert _snapshot(consumer.root) == before

        updated = install()
        assert "Updated apm.lock.yaml" in updated.stdout
        assert "already up to date" not in updated.stdout
        locked = load_yaml(lock_path)
        expected = (
            None
            if table is None
            else [replacement_rule_to_dict(parsed) for parsed in parse_replace_block(table)]
        )
        assert locked.get("replacements") == expected
        assert locked["dependencies"] == []
        assert locked["deployments"] == []
        if table is not None:
            assert locked["lockfile_version"] == "3"
        assert unmanaged.read_bytes() == b"Unmanaged content\n"
        assert not (consumer.root / "../intentionally-absent").exists()
        stable = _snapshot(consumer.root)
        unchanged = install()
        assert "already up to date" in unchanged.stdout
        install("--frozen")
        assert _snapshot(consumer.root) == stable


@pytest.mark.parametrize("keep_targets", [True, False], ids=["declared-targets", "no-target"])
def test_empty_graph_persists_dormant_replacement_table(
    tmp_path: Path, apm_binary_path: Path, keep_targets: bool
) -> None:
    _assert_empty_graph_replacement_lifecycle(
        tmp_path, ApmLifecycleRunner((str(apm_binary_path),)), keep_targets
    )


def test_replacement_upstream_local_fork_upstream_owns_one_slot(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert fixture.locked_entry()["resolved_commit"] == fixture.upstream_sha
    unmanaged = fixture.consumer.root / ".github/instructions/user.instructions.md"
    unmanaged.write_bytes(b"unmanaged user content\n")

    fixture.replace({"path": "../local-base"})
    fixture.install()
    fixture.assert_bytes("LOCAL")
    local_entry = fixture.locked_entry()
    assert local_entry["replacement"]["source"] == {"path": "../local-base"}
    assert not local_entry.get("resolved_commit")
    locked = load_yaml(fixture.consumer.root / "apm.lock.yaml")
    assert locked["lockfile_version"] == "3"
    assert parse_replace_block(locked["replacements"]) == parse_replace_block(
        load_yaml(fixture.consumer.manifest_path)["replace"]
    )

    fixture.replace({"git": _FORK, "ref": fixture.fork_sha})
    fixture.install()
    fixture.assert_bytes("FORK")
    fork_entry = fixture.locked_entry()
    assert fork_entry["resolved_commit"] == fixture.fork_sha
    assert fork_entry["replacement"]["requests"][0]["ref"] == fixture.upstream_sha

    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    restored = fixture.locked_entry()
    assert restored["resolved_commit"] == fixture.upstream_sha
    assert not restored.get("replacement")
    assert unmanaged.read_bytes() == b"unmanaged user content\n"


def test_replacement_same_named_ref_invalidates_upstream_cache_and_pin(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"][0]["ref"] = "main"
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    fixture.replace({"git": _FORK, "ref": "main"})
    fixture.install()
    fixture.assert_bytes("FORK")
    assert fixture.locked_entry()["resolved_commit"] == fixture.fork_sha
    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert fixture.locked_entry()["resolved_commit"] == fixture.upstream_sha


def test_replacement_monorepo_subpath_preserves_unmatched_siblings(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    packages = LocalPackageFactory(fixture.upstream.worktree / "packages")
    base = packages.create("base", targets=("copilot",))
    packages.add_instruction(base, "base", _content("UPSTREAM-SUBPATH"))
    sibling = packages.create("other", targets=("copilot",))
    packages.add_instruction(sibling, "other", _content("UNMATCHED-SIBLING"))
    selector = {"git": _UPSTREAM, "path": "packages/base"}
    packages.create("app", dependencies=({**selector, "ref": "main"},))
    upstream_sha = fixture.repositories.commit(
        fixture.upstream, message="Monorepo with a transitive package"
    ).sha
    patched = LocalPackageFactory(fixture.fork.worktree / "patched")
    fork_base = patched.create("base", targets=("copilot",))
    patched.add_instruction(fork_base, "base", _content("FORK-SUBPATH"))
    fork_sha = fixture.repositories.commit(
        fixture.fork, message="Replacement in a different subdirectory"
    ).sha
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"] = [
        {"git": _UPSTREAM, "path": f"packages/{name}", "ref": upstream_sha}
        for name in ("app", "other")
    ]
    modules = fixture.consumer.root / "apm_modules"
    expected_manifests = {
        DependencyReference.parse_from_dict(
            {"git": _UPSTREAM, "path": f"packages/{name}"}
        ).get_install_path(modules)
        / "apm.yml"
        for name in ("app", "base", "other")
    }
    for source, marker in (
        (None, "UPSTREAM-SUBPATH"),
        ({"path": "../local-base"}, "LOCAL"),
        ({"git": _FORK, "path": "patched/base", "ref": fork_sha}, "FORK-SUBPATH"),
        (None, "UPSTREAM-SUBPATH"),
    ):
        if source is None:
            manifest.pop("$schema", None)
            manifest.pop("replace", None)
        else:
            manifest["$schema"] = _SCHEMA
            manifest["replace"] = [{"for": selector, "with": source}]
        dump_yaml(manifest, fixture.consumer.manifest_path)
        fixture.install()
        fixture.assert_bytes(marker)
        assert (
            fixture.consumer.root / ".github/instructions/other.instructions.md"
        ).read_text() == _content("UNMATCHED-SIBLING")
        assert set(modules.rglob("apm.yml")) == expected_manifests
        entries = {
            entry["virtual_path"]: entry
            for entry in load_yaml(fixture.consumer.root / "apm.lock.yaml")["dependencies"]
        }
        assert set(entries) == {"packages/app", "packages/base", "packages/other"}
        assert "replacement" not in entries["packages/app"]
        assert "replacement" not in entries["packages/other"]
        assert entries["packages/other"]["resolved_commit"] == upstream_sha
        selected = entries["packages/base"]
        if source is None:
            assert "replacement" not in selected
            assert selected["resolved_commit"] == upstream_sha
        else:
            assert selected["replacement"]["source"] == source
            assert selected["replacement"]["requests"][0]["ref"] == "main"
            if "git" in source:
                assert selected["resolved_commit"] == fork_sha


def test_replacement_local_file_children_keep_selected_anchor_and_frozen_baseline(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Selected package children compose with ordinary single-file admission."""
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    sibling = fixture.local.root.parent / "selected.instructions.md"
    sibling.write_text(_content("SELECTED-CHILD"), encoding="utf-8")
    fixture.work_factory.replace_apm_dependencies(fixture.local, ("../selected.instructions.md",))
    unmatched = fixture.consumer.root / "unmatched.instructions.md"
    unmatched.write_text(_content("UNMATCHED-FILE"), encoding="utf-8")
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"].append({"path": "./unmatched.instructions.md"})
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    fixture.assert_bytes("LOCAL")
    deployed = fixture.consumer.root / ".github/instructions"
    assert (deployed / sibling.name).read_bytes() == sibling.read_bytes()
    assert (deployed / unmatched.name).read_bytes() == unmatched.read_bytes()
    lock_path = fixture.consumer.root / "apm.lock.yaml"
    baseline = lock_path.read_bytes()
    entries = {row["repo_url"]: row for row in load_yaml(lock_path)["dependencies"]}
    assert set(entries) == {
        "apm-replacement-fixture/base",
        "_local/selected.instructions.md",
        "_local/unmatched.instructions.md",
    }
    assert entries["apm-replacement-fixture/base"]["replacement"]["source"] == {
        "path": "../local-base"
    }
    child = entries["_local/selected.instructions.md"]
    assert child["local_path"] == "../selected.instructions.md"
    assert child["resolved_by"] == "apm-replacement-fixture/base"
    assert child["content_hash"].startswith("sha256:")
    assert "replacement" not in child
    assert "replacement" not in entries["_local/unmatched.instructions.md"]
    frozen = fixture.run(*_INSTALL, "--frozen")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert lock_path.read_bytes() == baseline

    sibling.write_text(_content("EDITED-CHILD"), encoding="utf-8")
    before = _snapshot(fixture.consumer.root)
    refused = fixture.run(*_INSTALL, "--frozen")
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "content differs" in " ".join((refused.stdout + refused.stderr).split())
    assert _snapshot(fixture.consumer.root) == before
    assert (deployed / sibling.name).read_text() == _content("SELECTED-CHILD")
    fixture.install()
    assert (deployed / sibling.name).read_bytes() == sibling.read_bytes()
    stable = _snapshot(fixture.consumer.root)
    audit = fixture.run("audit", "--ci", "--no-policy", "--format", "json")
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert json.loads(audit.stdout)["passed"] is True
    assert _snapshot(fixture.consumer.root) == stable

    actual = sibling.with_name("actual.instructions.md")
    sibling.rename(actual)
    sibling.symlink_to(actual)
    audit = fixture.run("audit", "--ci", "--no-policy", "--format", "json")
    assert audit.returncode != 0, audit.stdout + audit.stderr
    assert "symlink" in (audit.stdout + audit.stderr).lower()
    assert _snapshot(fixture.consumer.root) == stable
    sibling.unlink()
    actual.rename(sibling)

    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert not (deployed / sibling.name).exists()
    assert (deployed / unmatched.name).read_bytes() == unmatched.read_bytes()
    entries = {row["repo_url"]: row for row in load_yaml(lock_path)["dependencies"]}
    assert set(entries) == {"apm-replacement-fixture/base", "_local/unmatched.instructions.md"}
    assert "replacement" not in entries["apm-replacement-fixture/base"]
    assert entries["apm-replacement-fixture/base"]["resolved_commit"] == fixture.upstream_sha


@pytest.mark.parametrize("source_kind", ["local", "fork"])
def test_replacement_only_apm_records_selected_source_omission(
    tmp_path: Path, apm_binary_path: Path, source_kind: str
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    source_root = fixture.local.root if source_kind == "local" else fixture.fork.worktree
    manifest_path = source_root / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest.setdefault("dependencies", {})["mcp"] = [
        {
            "name": "selected-service",
            "registry": False,
            "transport": "stdio",
            "command": "inert-selected-command",
        }
    ]
    dump_yaml(manifest, manifest_path)
    if source_kind == "local":
        fixture.replace({"path": "../local-base"})
    else:
        pin = fixture.repositories.commit(fixture.fork, message="Selected-source MCP declaration")
        fixture.replace({"git": _FORK, "ref": pin.sha})

    result = fixture.run(*_INSTALL, "--only", "apm")
    assert result.returncode == 0, result.stdout + result.stderr
    lock_path = fixture.consumer.root / "apm.lock.yaml"
    locked = load_yaml(lock_path)
    assert locked["dependencies"][0]["repo_url"] == "apm-replacement-fixture/base"
    assert set(locked["mcp_omissions"]) == {"selected-service"}
    assert locked["mcp_omissions"]["selected-service"]["reason"] == "only-apm"
    assert not locked.get("mcp_configs")
    assert not locked.get("mcp_servers")
    before = _snapshot(fixture.consumer.root)
    result = fixture.run("audit", "--ci", "--no-policy", "--format", "json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["passed"] is True
    assert _snapshot(fixture.consumer.root) == before

    original_lock = lock_path.read_bytes()
    locked["mcp_omissions"]["selected-service"]["declaration"] = "sha256:" + "0" * 64
    dump_yaml(locked, lock_path)
    damaged = _snapshot(fixture.consumer.root)
    result = fixture.run("audit", "--ci", "--no-policy", "--format", "json")
    assert result.returncode != 0, result.stdout + result.stderr
    checks = json.loads(result.stdout)["checks"]
    consistency = next(check for check in checks if check["name"] == "config-consistency")
    assert consistency["passed"] is False
    assert any("selection is unknown" in detail for detail in consistency["details"])
    assert _snapshot(fixture.consumer.root) == damaged
    lock_path.write_bytes(original_lock)

    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert not load_yaml(lock_path).get("mcp_omissions")


@pytest.mark.parametrize("source_kind", ["local", "fork"])
def test_replacement_selected_skill_preflight_preserves_prior_install(
    tmp_path: Path, apm_binary_path: Path, source_kind: str
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    source_root = fixture.local.root if source_kind == "local" else fixture.fork.worktree
    selected_skill = source_root / ".apm/skills/review/SKILL.md"
    selected_skill.parent.mkdir(parents=True)
    selected_content = b"---\nname: review\ndescription: Selected source\n---\nSelected review.\n"
    selected_skill.write_bytes(selected_content)
    if source_kind == "local":
        selected_source = {"path": "../local-base"}
    else:
        pin = fixture.repositories.commit(fixture.fork, message="Selected-source review skill")
        selected_source = {"git": _FORK, "ref": pin.sha}
    fixture.replace(selected_source)

    user_skill = fixture.consumer.root / ".agents/skills/review/SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_content = b"---\nname: review\ndescription: Personal review\n---\nKeep my review.\n"
    user_skill.write_bytes(user_content)
    native_before = _native_bytes(fixture.consumer.root)
    state_before = {
        path: path.read_bytes()
        for path in (fixture.consumer.manifest_path, fixture.consumer.root / "apm.lock.yaml")
    }
    for _ in range(2):
        result = fixture.run()
        assert result.returncode != 0, result.stdout + result.stderr
        output = " ".join((result.stdout + result.stderr).split())
        assert "Installation refused" in output
        assert ".agents/skills/review/SKILL.md" in output
        assert "apm-replacement-fixture/base" in output
        assert "copilot" in output and "Rename" in output
        assert _native_bytes(fixture.consumer.root) == native_before
        assert {path: path.read_bytes() for path in state_before} == state_before
        assert selected_skill.read_bytes() == selected_content
        fixture.assert_bytes("UPSTREAM")

    personal = user_skill.parent.with_name("personal-review")
    user_skill.parent.rename(personal)
    fixture.install()
    fixture.assert_bytes("LOCAL" if source_kind == "local" else "FORK")
    assert user_skill.read_bytes() == selected_content
    assert (personal / "SKILL.md").read_bytes() == user_content
    assert fixture.locked_entry()["replacement"]["source"] == selected_source
    frozen = fixture.run(*_INSTALL, "--frozen")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert not user_skill.exists()
    assert (personal / "SKILL.md").read_bytes() == user_content


@pytest.mark.parametrize("dry_run", [False, True], ids=["install", "preview"])
def test_replacement_ignores_unused_original_http_transport(
    tmp_path: Path, apm_binary_path: Path, dry_run: bool
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    manifest = load_yaml(fixture.consumer.manifest_path)
    original = "http://github.com/apm-replacement-fixture/base"
    manifest["dependencies"]["apm"][0]["git"] = original
    manifest["replace"][0]["for"]["git"] = original
    dump_yaml(manifest, fixture.consumer.manifest_path)
    result = fixture.run(*_INSTALL, *(("--dry-run",) if dry_run else ()))
    assert result.returncode == 0, result.stdout + result.stderr
    if dry_run:
        assert not (fixture.consumer.root / "apm.lock.yaml").exists()
        assert not (fixture.consumer.root / _INSTRUCTION).exists()
    else:
        fixture.assert_bytes("LOCAL")
        assert fixture.locked_entry()["replacement"]["source"] == {"path": "../local-base"}
    fixture.replace(None)
    result = fixture.run(*_INSTALL, *(("--dry-run",) if dry_run else ()))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "insecure" in (result.stdout + result.stderr).lower()


def test_replacement_transitive_dry_run_keeps_consumer_anchor_and_outputs_untouched(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    nested = LocalPackageFactory(fixture.consumer.root.parent / "nested")
    nested.create("parent", dependencies=({"git": _UPSTREAM},), targets=("copilot",))
    fixture.replace({"path": "../local-base"})
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"] = ["../nested/parent"]
    dump_yaml(manifest, fixture.consumer.manifest_path)
    manifest_before = fixture.consumer.manifest_path.read_bytes()
    before = _snapshot(fixture.consumer.root)
    preview = fixture.run(*_INSTALL, "--dry-run")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "APM dependencies (2)" in preview.stdout
    assert "../local-base -> install" in preview.stdout
    assert "Dependency preview incomplete" not in preview.stdout
    assert _snapshot(fixture.consumer.root) == before
    assert fixture.consumer.manifest_path.read_bytes() == manifest_before
    fixture.replace({"path": "../missing"})
    missing_manifest = fixture.consumer.manifest_path.read_bytes()
    incomplete = fixture.run(*_INSTALL, "--dry-run", "--verbose")
    assert incomplete.returncode == 0, incomplete.stdout + incomplete.stderr
    assert "Dependency preview incomplete" in incomplete.stdout
    assert "Cannot load replacement" in " ".join((incomplete.stdout + incomplete.stderr).split())
    assert "Dry run completed: would" not in incomplete.stdout
    assert "Files that would be removed" not in incomplete.stdout
    assert _snapshot(fixture.consumer.root) == before
    assert fixture.consumer.manifest_path.read_bytes() == missing_manifest


@pytest.mark.parametrize("cli_consent", [False, True])
def test_replacement_http_source_cannot_borrow_original_or_global_consent(
    tmp_path: Path, apm_binary_path: Path, cli_consent: bool
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    fixture.replace({"git": "http://github.com/apm-replacement-fixture/fork", "ref": "main"})
    manifest = load_yaml(fixture.consumer.manifest_path)
    original = "http://github.com/apm-replacement-fixture/base"
    manifest["dependencies"]["apm"][0]["git"] = original
    manifest["dependencies"]["apm"][0]["allow_insecure"] = True
    manifest["replace"][0]["for"]["git"] = original
    dump_yaml(manifest, fixture.consumer.manifest_path)
    result = fixture.run(*_INSTALL, *(("--allow-insecure",) if cli_consent else ()))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "HTTP replacement sources are not supported" in " ".join(
        (result.stdout + result.stderr).split()
    )
    assert _snapshot(fixture.consumer.root) == before


@pytest.mark.parametrize("failure", ["missing", "invalid", "bad-fork-ref"])
def test_replacement_failure_preserves_previous_owned_bytes_without_fallback(
    tmp_path: Path, apm_binary_path: Path, failure: str
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    source = {"path": "../missing"}
    if failure == "invalid":
        fixture.local.manifest_path.write_text("name: [invalid\n", encoding="utf-8")
        source = {"path": "../local-base"}
    elif failure == "bad-fork-ref":
        source = {"git": _FORK, "ref": "missing-replacement-ref"}
    fixture.replace(source)
    result = fixture.run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert "replacement" in (result.stdout + result.stderr).lower()
    assert _snapshot(fixture.consumer.root) == before
    fixture.assert_bytes("LOCAL")


@pytest.mark.parametrize("cold", [False, True], ids=["existing", "cold"])
def test_replacement_frozen_rejects_original_commit_as_selected_fork_pin(
    tmp_path: Path, apm_binary_path: Path, cold: bool
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"git": _FORK, "ref": fixture.fork_sha})
    fixture.install()
    fixture.assert_bytes("FORK")
    target = fixture.consumer.root
    if cold:
        target = target.parent / "cold-consumer"
        target.mkdir()
        shutil.copy2(fixture.consumer.manifest_path, target / "apm.yml")
        shutil.copy2(fixture.consumer.root / "apm.lock.yaml", target / "apm.lock.yaml")
    lock_path = target / "apm.lock.yaml"
    locked = load_yaml(lock_path)
    assert locked["dependencies"][0]["resolved_commit"] == fixture.fork_sha
    locked["dependencies"][0]["resolved_commit"] = fixture.upstream_sha
    dump_yaml(locked, lock_path)
    before = _snapshot(target)
    result = fixture.run(*_INSTALL, "--frozen", root=target)
    assert result.returncode != 0, result.stdout + result.stderr
    output = " ".join((result.stdout + result.stderr).split())
    assert "--frozen: apm.lock.yaml is out of sync with apm.yml." in output
    assert f"manifest commit '{fixture.fork_sha}'" in output
    assert f"lockfile resolved_commit '{fixture.upstream_sha}'" in output
    assert _snapshot(target) == before


def test_replacement_selected_fork_children_keep_immutable_obligations(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    leaf = fixture.work_factory.create("leaf")
    repository = fixture.repositories.create("leaf", source_tree=leaf.root)
    first = fixture.repositories.commit(repository, message="First leaf")
    leaf_manifest = repository.worktree / "apm.yml"
    leaf_data = load_yaml(leaf_manifest)
    leaf_data["version"] = "1.0.1"
    dump_yaml(leaf_data, leaf_manifest)
    second = fixture.repositories.commit(repository, message="Conflicting leaf")
    remote = "https://github.com/apm-replacement-fixture/leaf"
    fork_manifest = fixture.fork.worktree / "apm.yml"
    fork_data = load_yaml(fork_manifest)
    fork_data["dependencies"] = {"apm": [{"git": remote, "ref": second.sha}]}
    dump_yaml(fork_data, fork_manifest)
    fork_pin = fixture.repositories.commit(fixture.fork, message="Fork requires second leaf")
    fixture.environment.update(
        fixture.repositories.url_rewrite_subprocess_env_many(
            ((fixture.upstream, _UPSTREAM), (fixture.fork, _FORK), (repository, remote))
        )
    )
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"].append({"git": remote, "ref": first.sha})
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.replace({"git": _FORK, "ref": fork_pin.sha})
    result = fixture.run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert "incompatible immutable" in " ".join((result.stdout + result.stderr).lower().split())
    assert _snapshot(fixture.consumer.root) == before
    fixture.assert_bytes("UPSTREAM")


@pytest.mark.parametrize("cold", [False, True], ids=["existing", "cold"])
@pytest.mark.parametrize(
    "failure", ["modified", "added", "removed", "missing-hash", "missing-source"]
)
def test_replacement_frozen_local_content_uses_locked_source_evidence(
    tmp_path: Path, apm_binary_path: Path, cold: bool, failure: str
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    fixture.assert_bytes("LOCAL")
    target = fixture.consumer.root
    if cold:
        target = target.parent / "cold-consumer"
        target.mkdir()
        shutil.copy2(fixture.consumer.manifest_path, target / "apm.yml")
        shutil.copy2(fixture.consumer.root / "apm.lock.yaml", target / "apm.lock.yaml")
    accepted = fixture.run(*_INSTALL, "--frozen", root=target)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    fixture.assert_bytes("LOCAL", root=target)
    original = fixture.local.root / ".apm/instructions/base.instructions.md"
    if failure == "modified":
        original.write_text(_content("LOCAL-CHANGED"), encoding="utf-8")
    elif failure == "added":
        original.with_name("added.instructions.md").write_text(_content("ADDED"), encoding="ascii")
    elif failure == "removed":
        original.unlink()
    elif failure == "missing-hash":
        lockfile_path = fixture.consumer.root / "apm.lock.yaml"
        lock = load_yaml(lockfile_path)
        lock["dependencies"][0].pop("content_hash")
        dump_yaml(lock, lockfile_path)
    else:
        fixture.local.root.rename(fixture.local.root.with_name("missing-selected-source"))
    if cold:
        target = fixture.consumer.root.parent / "changed-cold-consumer"
        target.mkdir()
        shutil.copy2(fixture.consumer.manifest_path, target / "apm.yml")
        shutil.copy2(fixture.consumer.root / "apm.lock.yaml", target / "apm.lock.yaml")
    before = _snapshot(target)
    refused = fixture.run(*_INSTALL, "--frozen", root=target)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    message = (refused.stdout + refused.stderr).lower()
    assert ("local" if failure == "missing-source" else "content") in message
    assert _snapshot(target) == before


@pytest.mark.parametrize("change", ["edit", "remove", "add"])
def test_replacement_frozen_compares_dormant_rules_without_reading_sources(
    tmp_path: Path, apm_binary_path: Path, change: str
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["replace"].append(
        {
            "for": {"git": "https://github.com/apm-replacement-fixture/unused"},
            "with": {"path": "../never-present"},
        }
    )
    dump_yaml(manifest, fixture.consumer.manifest_path)
    result = fixture.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unused" in (result.stdout + result.stderr).lower()
    before = _snapshot(fixture.consumer.root)
    if change == "edit":
        manifest["replace"][1]["with"]["path"] = "../also-not-present"
    elif change == "remove":
        manifest["replace"].pop()
    else:
        manifest["replace"].append(
            {
                "for": {"git": "https://github.com/apm-replacement-fixture/another-unused"},
                "with": {"path": "../also-not-present"},
            }
        )
    dump_yaml(manifest, fixture.consumer.manifest_path)
    result = fixture.run(*_INSTALL, "--frozen")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "replacement" in (result.stdout + result.stderr).lower()
    assert _snapshot(fixture.consumer.root) == before


def test_replacement_hook_consent_stays_with_selected_source(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    fixture.replace({"path": "../local-base"})
    fixture.work_factory.add_hook(
        fixture.local,
        "replacement-consent",
        {"hooks": {"preToolUse": [{"bash": "printf REPLACEMENT_HOOK"}]}},
    )
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["executables"] = {"allow": {"apm-replacement-fixture/base": {"hooks": True}}}
    dump_yaml(manifest, fixture.consumer.manifest_path)
    hooks = fixture.consumer.root / ".github/hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    sentinel = hooks / "user.json"
    sentinel.write_text('{"user":"preserve"}\n', encoding="ascii")
    fixture.install()
    fixture.assert_bytes("LOCAL")
    assert sorted(path.name for path in hooks.glob("*.json")) == ["user.json"]

    manifest["executables"]["allow"]["../local-base"] = {"hooks": True}
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.install()
    deployed = [path for path in hooks.glob("*.json") if path != sentinel]
    assert len(deployed) == 1
    assert "REPLACEMENT_HOOK" in deployed[0].read_text(encoding="utf-8")
    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert sorted(path.name for path in hooks.glob("*.json")) == ["user.json"]
    assert sentinel.read_bytes() == b'{"user":"preserve"}\n'


def test_replacement_source_symlink_escape_preserves_previous_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    outside = fixture.local.root.parent / "outside-package"
    outside.mkdir()
    marker = outside / "owned-fixture.txt"
    marker.write_text("DO_NOT_COPY\n", encoding="ascii")
    (fixture.local.root / "escape").symlink_to(outside, target_is_directory=True)
    fixture.replace({"path": "../local-base"})
    before = _snapshot(fixture.consumer.root)
    result = fixture.run(*_INSTALL, "--verbose")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "symlink" in (result.stdout + result.stderr).lower()
    assert _snapshot(fixture.consumer.root) == before
    fixture.assert_bytes("UPSTREAM")
    assert marker.read_bytes() == b"DO_NOT_COPY\n"


def test_replacement_explicit_fork_sha_survives_update(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"git": _FORK, "ref": fixture.fork_sha})
    fixture.install()
    fixture.assert_bytes("FORK")
    original = fixture.fork.worktree / ".apm/instructions/base.instructions.md"
    original.write_text(_content("FORK-NEW"), encoding="utf-8")
    fixture.repositories.commit(fixture.fork, message="Advance unselected replacement tip")
    upstream = fixture.upstream.worktree / ".apm/instructions/base.instructions.md"
    upstream.write_text(_content("UPSTREAM-NEW"), encoding="utf-8")
    upstream_tip = fixture.repositories.commit(fixture.upstream, message="New upstream release")
    fixture.repositories.tag(fixture.upstream, "v9.0.0", upstream_tip, annotated=True)
    before_manifest = fixture.consumer.manifest_path.read_bytes()
    result = fixture.run("update", "--yes", "--parallel-downloads", "0")
    assert result.returncode == 0, result.stdout + result.stderr
    fixture.assert_bytes("FORK")
    assert fixture.locked_entry()["resolved_commit"] == fixture.fork_sha
    assert fixture.consumer.manifest_path.read_bytes() == before_manifest


def test_replacement_mutable_fork_replays_pin_then_updates_selected_source(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"git": _FORK, "ref": "main"})
    fixture.install()
    before_manifest = fixture.consumer.manifest_path.read_bytes()
    instruction = fixture.fork.worktree / ".apm/instructions/base.instructions.md"
    instruction.write_text(_content("FORK-NEW"), encoding="utf-8")
    next_commit = fixture.repositories.commit(fixture.fork, message="Advance selected branch")
    fixture.install()
    fixture.assert_bytes("FORK")
    assert fixture.locked_entry()["resolved_commit"] == fixture.fork_sha
    before = _snapshot(fixture.consumer.root)
    outdated = fixture.run("outdated")
    assert outdated.returncode == 0, outdated.stdout + outdated.stderr
    assert _snapshot(fixture.consumer.root) == before
    updated = fixture.run("update", "--yes", "--parallel-downloads", "0")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    fixture.assert_bytes("FORK-NEW")
    assert fixture.locked_entry()["resolved_commit"] == next_commit.sha
    assert fixture.consumer.manifest_path.read_bytes() == before_manifest


def test_replacement_remote_diamond_preserves_original_requests_and_root_anchor(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    factory = LocalPackageFactory(fixture.consumer.root.parent)
    remotes = [(fixture.upstream, _UPSTREAM), (fixture.fork, _FORK)]
    direct = []
    originals = {}
    requests = {}
    for name, ref in (("left", fixture.upstream_sha), ("right", "f" * 40)):
        parent = factory.create(name, dependencies=({"git": _UPSTREAM, "ref": ref},))
        originals[parent.manifest_path] = parent.manifest_path.read_bytes()
        repository = fixture.repositories.create(name, source_tree=parent.root)
        sha = fixture.repositories.commit(repository, message=f"Diamond parent {name}").sha
        remote = f"https://github.com/apm-replacement-fixture/{name}"
        remotes.append((repository, remote))
        direct.append({"git": remote, "ref": sha})
        requests[f"apm-replacement-fixture/{name}"] = ref
    fixture.environment.update(fixture.repositories.url_rewrite_subprocess_env_many(remotes))
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"] = direct
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    fixture.assert_bytes("LOCAL")
    entries = load_yaml(fixture.consumer.root / "apm.lock.yaml")["dependencies"]
    assert len(entries) == 3
    replacement = next(d for d in entries if d["repo_url"] == "apm-replacement-fixture/base")
    assert {r["parent"]: r["ref"] for r in replacement["replacement"]["requests"]} == requests
    assert {path: path.read_bytes() for path in originals} == originals
    modules = fixture.consumer.root / "apm_modules"
    assert sorted(path.relative_to(modules).as_posix() for path in modules.rglob("apm.yml")) == [
        "apm-replacement-fixture/base/apm.yml",
        "apm-replacement-fixture/left/apm.yml",
        "apm-replacement-fixture/right/apm.yml",
    ]
    before = _snapshot(fixture.consumer.root)
    fixture.replace(None)
    refused = fixture.run()
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "incompatible immutable" in (refused.stdout + refused.stderr).lower()
    assert _snapshot(fixture.consumer.root) == before


def test_replacement_logical_cycle_fails_before_publishing_candidate(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    local = load_yaml(fixture.local.manifest_path)
    local["dependencies"] = {"apm": [{"git": _UPSTREAM, "ref": fixture.upstream_sha}]}
    dump_yaml(local, fixture.local.manifest_path)
    fixture.replace({"path": "../local-base"})
    refused = fixture.run()
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "circular" in (refused.stdout + refused.stderr).lower()
    assert _snapshot(fixture.consumer.root) == before


def test_replacement_physical_cycle_across_distinct_identities_preserves_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    other = "https://github.com/apm-replacement-fixture/other"
    local_manifest = load_yaml(fixture.local.manifest_path)
    local_manifest["dependencies"] = {"apm": [{"git": other}]}
    dump_yaml(local_manifest, fixture.local.manifest_path)
    fixture.replace({"path": "../local-base"})
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["replace"].append({"for": {"git": other}, "with": {"path": "../local-base/."}})
    dump_yaml(manifest, fixture.consumer.manifest_path)
    before = _snapshot(fixture.consumer.root)
    result = fixture.run()
    assert result.returncode != 0, result.stdout + result.stderr
    message = (result.stdout + result.stderr).lower()
    assert "circular" in message or "cycle" in message
    assert _snapshot(fixture.consumer.root) == before
    fixture.assert_bytes("UPSTREAM")


def test_replacement_preserves_alias_subset_targets_and_unmatched_dev_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    skills = {}
    for name in ("alpha", "beta"):
        skills[name] = f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n".encode()
        fixture.work_factory.add_skill(fixture.local, name, skills[name].decode())
    fixture.replace({"path": "../local-base"})
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"][0].update(
        {"alias": "chosen", "skills": ["alpha"], "targets": ["copilot"]}
    )
    manifest["devDependencies"] = {
        "apm": [
            {
                "git": _FORK,
                "ref": fixture.fork_sha,
                "alias": "dev-kit",
                "targets": ["claude"],
            }
        ]
    }
    dump_yaml(manifest, fixture.consumer.manifest_path)
    before = fixture.consumer.manifest_path.read_bytes()
    fixture.install()
    fixture.assert_bytes("LOCAL")
    modules = fixture.consumer.root / "apm_modules"
    assert sorted(path.relative_to(modules).as_posix() for path in modules.rglob("apm.yml")) == [
        "chosen/apm.yml",
        "dev-kit/apm.yml",
    ]
    assert (modules / "dev-kit/.apm/instructions/base.instructions.md").read_bytes() == _content(
        "FORK"
    ).encode()
    entries = {
        entry["repo_url"]: entry
        for entry in load_yaml(fixture.consumer.root / "apm.lock.yaml")["dependencies"]
    }
    request = entries["apm-replacement-fixture/base"]["replacement"]["requests"][0]
    assert (request["alias"], request["skills"], request["targets"], request["dev"]) == (
        "chosen",
        ["alpha"],
        ["copilot"],
        False,
    )
    assert entries["apm-replacement-fixture/fork"]["is_dev"] is True
    assert "replacement" not in entries["apm-replacement-fixture/fork"]
    deployed_skills = [
        path.read_bytes()
        for directory in (".github", ".agents", ".claude")
        for path in (fixture.consumer.root / directory).rglob("SKILL.md")
    ]
    assert deployed_skills == [skills["alpha"]]
    assert fixture.consumer.manifest_path.read_bytes() == before


def test_replacement_uninstall_preserves_shared_sources_until_the_last_parent(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    factory = LocalPackageFactory(fixture.consumer.root.parent)
    sibling = factory.create("sibling", targets=("copilot",))
    factory.add_instruction(sibling, "sibling", _content("SIBLING"))
    local_manifest = load_yaml(fixture.local.manifest_path)
    local_manifest["dependencies"] = {"apm": [{"path": "../sibling"}]}
    dump_yaml(local_manifest, fixture.local.manifest_path)
    for name, ref in (("left", fixture.upstream_sha), ("right", "other-original-request")):
        factory.create(name, dependencies=({"git": _UPSTREAM, "ref": ref},))
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"] = [{"path": "../left"}, {"path": "../right"}]
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    before_manifest = fixture.consumer.manifest_path.read_bytes()
    selected_manifest = fixture.local.manifest_path.read_bytes()
    fixture.local.manifest_path.unlink()
    refused = fixture.run("uninstall", "../left")
    fixture.local.manifest_path.write_bytes(selected_manifest)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "Cannot verify replacement dependency reachability" in " ".join(
        (refused.stdout + refused.stderr).split()
    )
    assert "(1 manifest(s): apm-replacement-fixture/base)" in " ".join(
        (refused.stdout + refused.stderr).split()
    )
    assert _snapshot(fixture.consumer.root) == before
    assert fixture.consumer.manifest_path.read_bytes() == before_manifest
    preview = fixture.run("uninstall", "../left", "--dry-run")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert _snapshot(fixture.consumer.root) == before
    assert fixture.consumer.manifest_path.read_bytes() == before_manifest
    first = fixture.run("uninstall", "../left")
    assert first.returncode == 0, first.stdout + first.stderr
    fixture.assert_bytes("LOCAL")
    sibling_output = fixture.consumer.root / ".github/instructions/sibling.instructions.md"
    assert sibling_output.read_bytes() == _content("SIBLING").encode("ascii")
    lock = load_yaml(fixture.consumer.root / "apm.lock.yaml")
    assert len(lock["dependencies"]) == 3
    selected = next(entry for entry in lock["dependencies"] if entry.get("replacement"))
    assert [edge["ref"] for edge in selected["replacement"]["requests"]] == [
        "other-original-request"
    ]
    second = fixture.run("uninstall", "../right")
    assert second.returncode == 0, second.stdout + second.stderr
    assert not (fixture.consumer.root / _INSTRUCTION).exists()
    assert not sibling_output.exists()
    remaining = load_yaml(fixture.consumer.root / "apm.lock.yaml")
    assert remaining["dependencies"] == []
    assert parse_replace_block(remaining["replacements"]) == parse_replace_block(
        load_yaml(fixture.consumer.manifest_path)["replace"]
    )


def test_replacement_rules_in_dependency_manifest_do_not_redirect_consumer(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    factory = LocalPackageFactory(fixture.consumer.root.parent)
    parent = factory.create(
        "parent", dependencies=({"git": _UPSTREAM, "ref": fixture.upstream_sha},)
    )
    authored = load_yaml(parent.manifest_path)
    authored["$schema"] = _SCHEMA
    authored["replace"] = [
        {"for": {"git": _UPSTREAM}, "with": {"path": "../forbidden-consumer-local"}}
    ]
    dump_yaml(authored, parent.manifest_path)
    original = parent.manifest_path.read_bytes()
    repository = fixture.repositories.create("parent", source_tree=parent.root)
    parent_sha = fixture.repositories.commit(repository, message="Dependency-owned rule").sha
    remote = "https://github.com/apm-replacement-fixture/parent"
    fixture.environment.update(
        fixture.repositories.url_rewrite_subprocess_env_many(
            ((fixture.upstream, _UPSTREAM), (repository, remote))
        )
    )
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"] = [{"git": remote, "ref": parent_sha}]
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    entries = load_yaml(fixture.consumer.root / "apm.lock.yaml")["dependencies"]
    assert len(entries) == 2
    assert all(not entry.get("replacement") for entry in entries)
    assert parent.manifest_path.read_bytes() == original


@pytest.mark.parametrize("cold", [False, True], ids=["existing", "cold"])
def test_remote_parent_replacement_local_sibling_frozen_preflight(
    tmp_path: Path, apm_binary_path: Path, cold: bool
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    factory = LocalPackageFactory(fixture.consumer.root.parent)
    sibling = factory.create("sibling", targets=("copilot",))
    factory.add_instruction(sibling, "sibling", _content("SIBLING"))
    local_manifest = load_yaml(fixture.local.manifest_path)
    local_manifest["dependencies"] = {"apm": [{"path": "../sibling"}]}
    dump_yaml(local_manifest, fixture.local.manifest_path)
    parent = factory.create("parent", dependencies=({"git": _UPSTREAM, "ref": "upstream"},))
    repository = fixture.repositories.create("parent", source_tree=parent.root)
    parent_sha = fixture.repositories.commit(repository, message="Remote parent").sha
    remote = "https://github.com/apm-replacement-fixture/parent"
    fixture.environment.update(
        fixture.repositories.url_rewrite_subprocess_env_many(
            ((fixture.upstream, _UPSTREAM), (repository, remote))
        )
    )
    manifest = load_yaml(fixture.consumer.manifest_path)
    manifest["dependencies"]["apm"] = [{"git": remote, "ref": parent_sha}]
    dump_yaml(manifest, fixture.consumer.manifest_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    target = fixture.consumer.root
    if cold:
        target = target.parent / "cold"
        target.mkdir()
        shutil.copy2(fixture.consumer.manifest_path, target / "apm.yml")
        shutil.copy2(fixture.consumer.root / "apm.lock.yaml", target / "apm.lock.yaml")
    accepted = fixture.run(*_INSTALL, "--frozen", root=target)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    fixture.assert_bytes("LOCAL", root=target)
    assert (target / ".github/instructions/sibling.instructions.md").read_text() == _content(
        "SIBLING"
    )
    (sibling.root / ".apm/instructions/sibling.instructions.md").write_text(
        _content("CHANGED"), encoding="utf-8"
    )
    before = _snapshot(target)
    refused = fixture.run(*_INSTALL, "--frozen", root=target)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "content" in (refused.stdout + refused.stderr).lower()
    assert _snapshot(target) == before


def test_replacement_local_audit_and_outdated_are_read_only(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    outdated = fixture.run("outdated", "--parallel-checks", "0")
    assert outdated.returncode == 0, outdated.stdout + outdated.stderr
    assert "not remotely comparable" in outdated.stdout
    assert "not-comparable" in outdated.stdout
    assert "local-base" in outdated.stdout
    package_column = "".join(
        line.split("|")[1].strip() for line in outdated.stdout.splitlines() if line.startswith("|")
    )
    assert "apm-replacement-fixture/base" in package_column
    assert "Replacement:" not in outdated.stdout
    verbose = fixture.run("outdated", "--parallel-checks", "0", "--verbose")
    assert verbose.returncode == 0, verbose.stdout + verbose.stderr
    assert "Replacement:" in verbose.stdout
    assert "apm-replacement-fixture/base" in verbose.stdout
    assert "local-base" in verbose.stdout
    assert _snapshot(fixture.consumer.root) == before
    audit = fixture.run("audit", "--ci", "--no-policy")
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert _snapshot(fixture.consumer.root) == before
    fixture.local.root.rename(fixture.local.root.with_name("missing-local"))
    refused = fixture.run("audit", "--ci", "--no-policy")
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "replacement source missing" in (refused.stdout + refused.stderr).lower()
    assert _snapshot(fixture.consumer.root) == before


@pytest.mark.parametrize("damage", ["modified", "deleted"])
def test_replacement_audit_detects_deployed_drift_without_repair(
    tmp_path: Path, apm_binary_path: Path, damage: str
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    deployed = fixture.consumer.root / _INSTRUCTION
    original = deployed.read_bytes()
    if damage == "modified":
        deployed.write_bytes(original + b"\nChanged outside APM\n")
    else:
        deployed.unlink()
    before = _snapshot(fixture.consumer.root)
    audit = fixture.run("audit", "--ci", "--no-policy")
    assert audit.returncode != 0, audit.stdout + audit.stderr
    assert _snapshot(fixture.consumer.root) == before
    deployed.write_bytes(original)
    restored = fixture.run("audit", "--ci", "--no-policy")
    assert restored.returncode == 0, restored.stdout + restored.stderr


def test_replacement_native_plugin_cache_and_changed_output_cleanup(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    write_agent_plugin(fixture.local.root, name="local-base", with_mcp=False)
    fixture.local.manifest_path.unlink()
    settings_path = fixture.consumer.root / ".github/copilot/settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"enabledPlugins": {"user-plugin@manual": False}}), encoding="ascii"
    )
    fixture.replace({"path": "../local-base"})
    fixture.install()
    assert not (fixture.consumer.root / _INSTRUCTION).exists()
    modules = fixture.consumer.root / "apm_modules"
    catalog = catalog_path_for(modules)
    (entry,) = json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
    assert entry["name"] == "local-base"
    assert Path(entry["source"]).parts == ("apm-replacement-fixture", "base")
    installed_skill = modules / entry["source"] / "skills/local-base-skill/SKILL.md"
    expected_skill = (fixture.local.root / "skills/local-base-skill/SKILL.md").read_bytes()
    assert installed_skill.read_bytes() == expected_skill
    clean = fixture.run("audit", "--ci", "--no-policy")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    installed_skill.write_bytes(expected_skill + b"\nTampered selected cache.\n")
    before = _snapshot(fixture.consumer.root)
    refused = fixture.run("audit", "--ci", "--no-policy")
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "cache differs" in " ".join((refused.stdout + refused.stderr).split())
    assert _snapshot(fixture.consumer.root) == before
    fixture.install()
    assert installed_skill.read_bytes() == expected_skill
    fixture.replace({"git": _FORK, "ref": fixture.fork_sha})
    fixture.install()
    fixture.assert_bytes("FORK")
    assert not installed_skill.exists()
    if catalog.exists():
        assert json.loads(catalog.read_text(encoding="utf-8"))["plugins"] == []
    assert (
        json.loads(settings_path.read_text(encoding="utf-8"))["enabledPlugins"][
            "user-plugin@manual"
        ]
        is False
    )
    fixture.replace(None)
    fixture.install()
    fixture.assert_bytes("UPSTREAM")
    assert (
        json.loads(settings_path.read_text(encoding="utf-8"))["enabledPlugins"][
            "user-plugin@manual"
        ]
        is False
    )


def test_local_replacement_update_plans_changed_content_without_dry_run_writes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    previous_hash = fixture.locked_entry()["content_hash"]
    (fixture.local.root / ".apm/instructions/base.instructions.md").write_text(
        _content("UPDATED-LOCAL"), encoding="utf-8"
    )
    preview = fixture.run("update", "--dry-run", "--parallel-downloads", "0")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "local content" in preview.stdout.lower()
    assert _snapshot(fixture.consumer.root) == before
    updated = fixture.run("update", "--yes", "--parallel-downloads", "0")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    fixture.assert_bytes("UPDATED-LOCAL")
    assert fixture.locked_entry()["content_hash"] != previous_hash


def test_partial_update_refuses_changed_replacement_table_before_mutation(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.install()
    before = _snapshot(fixture.consumer.root)
    fixture.replace({"path": "../local-base"})
    refused = fixture.run(
        "update", "apm-replacement-fixture/base", "--yes", "--parallel-downloads", "0"
    )
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "complete replacement table" in " ".join(
        (refused.stdout + refused.stderr).lower().split()
    )
    assert _snapshot(fixture.consumer.root) == before


def test_existing_local_replacement_reloads_changed_child_manifest(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    fixture = _replacement_fixture(tmp_path, apm_binary_path)
    fixture.replace({"path": "../local-base"})
    fixture.install()
    factory = LocalPackageFactory(fixture.consumer.root.parent)
    child = factory.create("child", targets=("copilot",))
    factory.add_instruction(child, "child", _content("CHILD"))
    local = load_yaml(fixture.local.manifest_path)
    local["dependencies"] = {"apm": [{"path": "../child"}]}
    dump_yaml(local, fixture.local.manifest_path)
    fixture.install()
    deployed = fixture.consumer.root / ".github/instructions/child.instructions.md"
    assert deployed.read_text() == _content("CHILD")
    entries = load_yaml(fixture.consumer.root / "apm.lock.yaml")["dependencies"]
    assert len(entries) == 2
    local.pop("dependencies")
    dump_yaml(local, fixture.local.manifest_path)
    fixture.install()
    assert not deployed.exists()
    fixture.assert_bytes("LOCAL")
    entries = load_yaml(fixture.consumer.root / "apm.lock.yaml")["dependencies"]
    assert len(entries) == 1
    assert entries[0]["repo_url"] == "apm-replacement-fixture/base"
