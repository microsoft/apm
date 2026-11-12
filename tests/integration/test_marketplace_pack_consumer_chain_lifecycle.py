"""Marketplace producer -> consumer chain keeps disk, lock and exit codes coherent (#3649).

Covers #3534 (sha pins, malformed sha refused before any write), #3543
(marketplace check on commit-SHA refs), #1994 (registered ref), #2637
(--category into apm.yml and Codex output), #2162 (export-patch hint on
drift), plus pack determinism, frozen cold-cache restore, uninstall, prune.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    lock_dep,
    output,
    remote_url,
    skill_md,
    tree_bytes,
    write,
)
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_OUTPUTS = {
    "claude": ".claude-plugin/marketplace.json",
    "codex": ".agents/plugins/marketplace.json",
    "copilot": ".github/plugin/marketplace.json",
}


def _plugins(path: Path) -> dict[str, dict]:
    return {p["name"]: p for p in json.loads(path.read_text())["plugins"]}


def _build(tmp_path: Path, binary: Path) -> tuple[MarketplacePackWorld, dict]:
    world = MarketplacePackWorld.create(tmp_path, binary)
    src = world.isolated.package_root

    skillpack = src / "skillpack"
    write(skillpack / "apm.yml", "name: skillpack\nversion: 1.0.0\ndescription: s\n")
    write(skillpack / "skills" / "alpha" / "SKILL.md", skill_md("alpha", "see ref.md\n"))
    write(skillpack / "shared" / "ref.md", "shared reference\n")
    write(skillpack / ".apm" / "agents" / "rev.agent.md", "---\ndescription: r\n---\n# rev\n")
    sp_repo = world.repos.create("skillpack", source_tree=skillpack)
    # copytree follows links; author the contained symlink in the git worktree.
    (sp_repo.worktree / "skills" / "alpha" / "ref.md").symlink_to("../../shared/ref.md")
    sp_v1 = world.repos.commit(sp_repo, message="seed skillpack")
    world.rewrites.append((sp_repo, remote_url("skillpack")))
    world.env = world.repos.url_rewrite_subprocess_env_many(world.rewrites)
    world.repos.tag(sp_repo, "v1.0.0", sp_v1)

    rawplug = src / "rawplug"
    write(rawplug / ".claude-plugin" / "plugin.json", '{"name": "rawplug", "version": "0.4.0"}\n')
    write(rawplug / "skills" / "rx" / "SKILL.md", skill_md("rx"))
    write(rawplug / "agents" / "ra.md", "---\nname: ra\ndescription: a\n---\n# ra\n")
    _, rp_v1 = world.publish("rawplug", rawplug)

    producer = src / "market"
    write(producer / "packages" / "local-tool" / "apm.yml", "name: local-tool\nversion: 0.1.0\n")
    write(producer / "packages" / "local-tool" / "skills" / "lt" / "SKILL.md", skill_md("lt"))
    manifest = {
        "name": "Chain Market",
        "version": "1.0.0",
        "description": "chain",
        "marketplace": {
            "owner": {"name": "acme", "url": "https://example.invalid/acme"},
            "outputs": {"claude": {}, "codex": {}, "copilot": {}},
            "build": {"tagPattern": "v{version}"},
            "packages": [
                {
                    "name": "skillpack",
                    "description": "skills",
                    "source": "git.example.invalid/acme/skillpack",
                    "ref": "v1.0.0",
                    "version": "1.0.0",
                    "category": "Productivity",
                },
                {
                    "name": "rawplug",
                    "description": "raw plugin",
                    "source": "git.example.invalid/acme/rawplug",
                    "ref": rp_v1.sha,
                    "version": "0.4.0",
                    "category": "Productivity",
                },
                {
                    "name": "local-tool",
                    "source": "./packages/local-tool",
                    "version": "0.1.0",
                    "category": "Productivity",
                },
            ],
        },
    }
    dump_yaml(manifest, producer / "apm.yml")
    return world, {
        "skillpack": skillpack,
        "sp_repo": sp_repo,
        "sp_v1": sp_v1.sha,
        "rp_v1": rp_v1.sha,
        "producer": producer,
    }


def _publish_producer(world: MarketplacePackWorld, ctx: dict) -> None:
    producer = ctx["producer"]
    if "m_repo" not in ctx:
        ctx["m_repo"], _ = world.publish("market", producer)
        return
    shutil.copytree(
        producer,
        ctx["m_repo"].worktree,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    world.repos.commit(ctx["m_repo"], message="republish")


def test_marketplace_pack_to_consumer_chain(tmp_path: Path, apm_binary_path: Path) -> None:
    world, ctx = _build(tmp_path, apm_binary_path)
    producer = ctx["producer"]

    # 1. Producer pack: every configured output, sha pins, category, deterministic.
    world.apm(producer, "pack")
    first = {fmt: (producer / rel).read_bytes() for fmt, rel in _OUTPUTS.items()}
    claude = _plugins(producer / _OUTPUTS["claude"])
    assert claude["skillpack"]["source"]["sha"] == ctx["sp_v1"]
    assert claude["rawplug"]["source"]["sha"] == ctx["rp_v1"]
    codex = _plugins(producer / _OUTPUTS["codex"])
    assert {p["category"] for p in codex.values()} == {"Productivity"}
    world.apm(producer, "pack")
    assert {fmt: (producer / rel).read_bytes() for fmt, rel in _OUTPUTS.items()} == first
    world.apm(producer, "pack", "--check-clean")
    world.apm(producer, "marketplace", "check")
    _publish_producer(world, ctx)

    # 2. Consumer: register at a ref, validate, install every plugin.
    project = world.consumer("chain-consumer", targets=("claude", "copilot"))
    world.apm(project, "marketplace", "add", remote_url("market"), "--name", "mk", "--ref", "main")
    world.apm(project, "marketplace", "validate", "mk")
    for plugin in ("skillpack", "rawplug", "local-tool"):
        world.apm(project, "install", f"{plugin}@mk", "--no-policy")
    lock = world.lock(project)
    assert lock_dep(lock, "acme/skillpack")["resolved_commit"] == ctx["sp_v1"]
    assert lock_dep(lock, "acme/rawplug")["resolved_commit"] == ctx["rp_v1"]
    lt = lock_dep(lock, "acme/market", "packages/local-tool")
    assert {d["discovered_via"] for d in lock["dependencies"]} == {"mk"}
    for rel in (
        ".claude/skills/alpha/SKILL.md",
        ".claude/skills/alpha/ref.md",
        ".claude/skills/rx/SKILL.md",
        ".claude/skills/lt/SKILL.md",
        ".github/agents/rev.agent.md",
    ):
        assert (project / rel).is_file(), rel
    assert not (project / ".claude/skills/alpha/ref.md").is_symlink()
    assert (project / ".claude/skills/alpha/ref.md").read_text() == "shared reference\n"

    # 3. Idempotent re-run: lockfile bytes do not churn; audit is clean.
    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    world.apm(project, "install", "--no-policy")
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    world.apm(project, "audit", "--ci", "--no-policy")

    # 4. Fresh clone, cold cache, --frozen restore, audit.
    world.git(project, "init", "-q")
    world.git(project, "add", "-A")
    world.git(project, "commit", "-qm", "consumer")
    clone = world.isolated.work_root / "chain-clone"
    world.git(world.isolated.work_root, "clone", "-q", str(project), str(clone))
    for entry in world.isolated.cache_root.iterdir():
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    world.apm(clone, "install", "--frozen", "--no-policy")
    assert (clone / "apm.lock.yaml").read_bytes() == lock_bytes
    world.apm(clone, "audit", "--ci", "--no-policy")
    assert world.git(clone, "status", "--porcelain").stdout.strip() == ""

    # 5. Drift -> audit hint -> export-patch (read-only) -> install hint.
    for rel in (".claude/skills/alpha/SKILL.md", ".claude/skills/rx/SKILL.md"):
        (clone / rel).write_text((clone / rel).read_text() + "consumer edit\n")
    audit = world.apm(clone, "audit", "--ci", "--no-policy", expect=1)
    assert "apm export-patch" in output(audit)
    before = ArtifactSnapshot.capture(clone)
    exported = world.apm(clone, "export-patch", "-o", str(tmp_path / "patches"))
    assert_unchanged(before, ArtifactSnapshot.capture(clone))
    assert len(list((tmp_path / "patches").glob("*.patch"))) == 2, output(exported)
    install = world.apm(clone, "install", "--no-policy")
    assert "apm export-patch" in output(install)
    assert (clone / "apm.lock.yaml").read_bytes() == lock_bytes

    # 6. Uninstall exactly what was installed; prune leaves nothing orphaned.
    world.apm(project, "uninstall", "skillpack@mk")
    lock = world.lock(project)
    assert all(d["repo_url"] != "acme/skillpack" for d in lock["dependencies"])
    assert not (project / ".claude/skills/alpha").exists()
    assert not (project / ".github/agents/rev.agent.md").exists()
    assert (project / ".claude/skills/rx/SKILL.md").is_file()
    assert (
        lt["resolved_commit"]
        == lock_dep(lock, "acme/market", "packages/local-tool")["resolved_commit"]
    )
    world.apm(project, "prune")
    deps = world.apm(project, "deps", "list")
    assert "orphaned" not in output(deps).lower()


@pytest.mark.parametrize(
    "bad_sha",
    ["XYZ", "3c79fd2", "--upload-pack=touch", "../../etc/passwd", "A" * 40, "a" * 41],
)
def test_malformed_marketplace_sha_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path, bad_sha: str
) -> None:
    world, ctx = _build(tmp_path, apm_binary_path)
    producer = ctx["producer"]
    world.apm(producer, "pack")
    for rel in _OUTPUTS.values():
        data = json.loads((producer / rel).read_text())
        for plugin in data["plugins"]:
            if plugin["name"] == "skillpack":
                plugin["source"]["sha"] = bad_sha
        (producer / rel).write_text(json.dumps(data, indent=2) + "\n")
    _publish_producer(world, ctx)

    project = world.consumer("tamper-consumer", targets=("claude",))
    world.apm(project, "marketplace", "add", remote_url("market"), "--name", "mk")
    before_project = tree_bytes(project)
    result = world.apm(project, "install", "skillpack@mk", "--no-policy", expect=None)
    assert result.returncode != 0, output(result)
    assert tree_bytes(project) == before_project, output(result)
    assert not (project / "apm.lock.yaml").exists()
    assert not (project / "apm_modules").exists()


def test_producer_bump_repack_then_consumer_update(tmp_path: Path, apm_binary_path: Path) -> None:
    world, ctx = _build(tmp_path, apm_binary_path)
    producer = ctx["producer"]
    world.apm(producer, "pack")
    _publish_producer(world, ctx)
    project = world.consumer("bump-consumer", targets=("claude",))
    world.apm(project, "marketplace", "add", remote_url("market"), "--name", "mk")
    world.apm(project, "install", "skillpack@mk", "--no-policy")
    assert lock_dep(world.lock(project), "acme/skillpack")["resolved_commit"] == ctx["sp_v1"]

    # Producer: new skill release and re-pack against the new tag.
    sp_repo = ctx["sp_repo"]
    write(sp_repo.worktree / "skills" / "beta" / "SKILL.md", skill_md("beta"))
    manifest = load_yaml(sp_repo.worktree / "apm.yml")
    manifest["version"] = "1.1.0"
    dump_yaml(manifest, sp_repo.worktree / "apm.yml")
    v11 = world.repos.commit(sp_repo, message="v1.1.0")
    world.repos.tag(sp_repo, "v1.1.0", v11)
    manifest = load_yaml(producer / "apm.yml")
    manifest["marketplace"]["packages"][0]["ref"] = "v1.1.0"
    manifest["marketplace"]["packages"][0]["version"] = "1.1.0"
    dump_yaml(manifest, producer / "apm.yml")
    world.apm(producer, "pack")
    assert _plugins(producer / _OUTPUTS["claude"])["skillpack"]["source"]["sha"] == v11.sha
    world.apm(producer, "marketplace", "check")
    _publish_producer(world, ctx)

    world.apm(project, "marketplace", "update", "mk")
    world.apm(project, "install", "skillpack@mk", "--no-policy")
    dep = lock_dep(world.lock(project), "acme/skillpack")
    assert dep["resolved_commit"] == v11.sha
    assert dep["version"] == "1.1.0"
    assert (project / ".claude/skills/beta/SKILL.md").is_file()
    assert (project / ".claude/skills/alpha/SKILL.md").is_file()
    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    world.apm(project, "install", "--no-policy")
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    world.apm(project, "audit", "--ci", "--no-policy")
