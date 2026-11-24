"""APMLifecycle: install applies manifest.scripts, unmanaged_files and max_depth (#4826).

PROMISE: P-INV-I7 -- a refused command leaves durable state as it found it.
Under ``enforcement: block``, ``apm install`` refuses when the manifest it
installs carries a ``scripts:`` block that ``manifest.scripts: deny`` forbids
(before any fetch, also under ``--root``; ``-g`` is a commit_gate unit row), when the resolved
graph is deeper than ``dependencies.max_depth`` (before anything past the
limit is fetched), or when the install would leave a governed file no lock
row claims under ``unmanaged_files.action: deny``. Each refusal names the
policy field and the fix and leaves project, lockfile (and its deployment
ledger), HOME, APM_HOME and the cache byte-identical; a refusal judged on
acquired content may leave only the content-addressed git checkout cache.
A control without the offending input installs.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.known import KnownFailure
from tests.lifecycle.world import World
from tests.lifecycle.world.builder import _pack_bundle
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.lifecycle.world.packages import _skill_package
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import policy_org
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_HAND_WRITTEN = ".github/instructions/hand-written.instructions.md"
_SCRIPTS = ("manifest.scripts: deny", "Remove the scripts: block from apm.yml")
_UNMANAGED = ("unmanaged_files.action: deny", "Move or delete each listed file")
_DEPTH = ("dependencies.max_depth", "Reduce the dependency nesting")


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str, scope: str = "project") -> CommandResult:
    return driver.run(world, list(args), scope=scope, entry_point=driver.driver_command("binary"))


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _assert_ok(world: World, *args: str, scope: str = "project") -> CommandResult:
    result = _apm(world, *args, scope=scope)
    assert result.returncode == 0, _evidence(result)
    return result


def _state(world: World, extra: Path | None, *, keep_git_cache: bool) -> dict[str, bytes]:
    """Every byte a refused install could leave: project, HOME, APM_HOME and cache.

    *keep_git_cache* drops only the content-addressed git checkout cache, which
    a refusal judged on acquired content leaves as every such refusal does;
    policy_v1, deploy_v1 and the rest of the cache stay in the oracle.
    """
    roots = {"project": world.project, "home": world.home, "apm_home": world.apm_home}
    if world.cache_root is not None:
        roots["cache"] = world.cache_root
    if extra is not None:
        roots["extra"] = extra
    state = {
        f"{name}/{path.relative_to(root).as_posix()}": path.read_bytes()
        for name, root in roots.items()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }
    if keep_git_cache:
        state = {key: data for key, data in state.items() if not key.startswith("cache/git/")}
    return state


def _assert_refused(
    world: World,
    expected: tuple[str, str],
    *args: str,
    scope: str = "project",
    extra: Path | None = None,
    keep_git_cache: bool = False,
) -> str:
    before = _state(world, extra, keep_git_cache=keep_git_cache)
    result = _apm(world, "install", *args, scope=scope)
    text = _text(result)
    assert result.returncode == 1, _evidence(result)
    assert expected[0] in text, text
    assert expected[1] in text, text
    assert text.isascii(), text
    after = _state(world, extra, keep_git_cache=keep_git_cache)
    assert sorted(set(after) ^ set(before)) == []
    assert [key for key in before if after[key] != before[key]] == []
    return text


def _seed(world: World, *args: str, package: str = "p-child") -> None:
    """Install one compliant package under the policy first.

    apm.lock.yaml and its deployment ledger then exist, and the policy cache
    holds the policy, so the refused install is judged against both unchanged.
    """
    _assert_ok(world, "install", *args, world.packages[package].spec)


def _add_scripts(manifest: Path) -> None:
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
    data = data or {"name": "user", "version": "1.0.0"}
    data["scripts"] = {"hello": "echo hi"}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _rules_bundle(world: World) -> Path:
    """Pack a bundle whose instruction deploys into a governed directory."""
    source = world.root / "rules-src"
    rules = source / ".apm" / "instructions" / "rules.instructions.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("---\napplyTo: '**'\n---\n# Rules\n", encoding="utf-8")
    manifest = "name: p-rules\nversion: 1.0.0\nlicense: MIT\ndependencies:\n  apm: []\n"
    (source / "apm.yml").write_text(manifest, encoding="utf-8")
    return _pack_bundle(source, world.root / "rules-bundle", world.env)


def _govern(world: World, directory: Path, variant: str) -> None:
    """Give *directory* the org remote whose policy is *variant*, as use_policy does."""
    git = ("git", "-C", str(directory))
    subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remote = f"https://github.com/{policy_org(variant)}/consumer.git"
    subprocess.run((*git, "remote", "add", "origin", remote), env=world.env, check=True)


@pytest.mark.parametrize("offending", [True, False], ids=["scripts-block", "control"])
def test_scripts_deny_refuses_before_any_fetch(
    lifecycle_world: World,  # noqa: F811
    offending: bool,
) -> None:
    world = lifecycle_world
    spec = world.packages["p-skill:git"].spec
    use_policy(world, "scripts-deny")
    if offending:
        _seed(world)
        assert (world.project / "apm.lock.yaml").is_file()
        _add_scripts(world.project / "apm.yml")

    if offending:
        text = _assert_refused(world, _SCRIPTS, spec)
        assert "hello" in text, text
    else:
        _assert_ok(world, "install", spec)


def test_scripts_deny_refuses_an_install_with_no_dependency_work(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "scripts-deny")
    _apm(world, "audit", "--ci")  # discovers the policy once, so its cache is warm
    _add_scripts(world.project / "apm.yml")

    _assert_refused(world, _SCRIPTS)


def test_scripts_deny_refuses_an_mcp_install(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "scripts-deny")
    _apm(world, "audit", "--ci")  # discovers the policy once, so its cache is warm
    _add_scripts(world.project / "apm.yml")

    _assert_refused(world, _SCRIPTS, "--mcp", "srv", "--url", "https://mcp.example.com/mcp")


def test_scripts_deny_refuses_a_local_bundle_install(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "scripts-deny")
    _seed(world)
    _add_scripts(world.project / "apm.yml")

    _assert_refused(world, _SCRIPTS, world.packages["p-exec:bundle"].spec)


def test_scripts_deny_judges_the_manifest_root_redirects_from(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    target = world.root / "redirected"
    target.mkdir()
    _govern(world, target, "scripts-deny")
    use_policy(world, "scripts-deny")
    _seed(world, "--root", str(target))
    _add_scripts(world.project / "apm.yml")

    spec = world.packages["p-skill:git"].spec
    _assert_refused(world, _SCRIPTS, "--root", str(target), spec, extra=target)


_MCP_ENTRY = ("--mcp", "srv", "--url", "https://mcp.example.com/mcp")


def _entry(world: World, entry: str) -> tuple[str, ...]:
    """The install arguments of one entry: ``mcp``, ``bundle`` or a dependency."""
    if entry == "mcp":
        return _MCP_ENTRY
    if entry == "bundle":
        return (world.packages["p-exec:bundle"].spec,)
    return (world.packages["p-skill:git"].spec,)


@pytest.mark.parametrize("entry", ["mcp", "dependency"])
def test_scripts_deny_is_read_from_the_source_manifest_under_root(
    lifecycle_world: World,  # noqa: F811
    entry: str,
) -> None:
    """Only the source checkout carries the policy: --root still refuses its scripts: block."""
    world = lifecycle_world
    target = world.root / "redirected"
    target.mkdir()
    use_policy(world, "scripts-deny")
    _apm(world, "audit", "--ci")  # discovers the policy once, so its cache is warm
    _add_scripts(world.project / "apm.yml")

    _assert_refused(world, _SCRIPTS, "--root", str(target), *_entry(world, entry), extra=target)


@pytest.mark.parametrize("entry", ["mcp", "dependency"])
def test_unmanaged_files_deny_is_judged_at_the_root_install_writes_to(
    lifecycle_world: World,  # noqa: F811
    entry: str,
) -> None:
    """Only the --root destination carries the policy and holds an unmanaged file."""
    world = lifecycle_world
    target = world.root / "redirected"
    target.mkdir()
    _govern(world, target, "unmanaged-deny")
    path = target / _HAND_WRITTEN
    path.parent.mkdir(parents=True)
    path.write_text("# hand written\n", encoding="utf-8")
    # A preview discovers the destination's policy once, so its cache is warm.
    _apm(world, "install", "--dry-run", "--root", str(target), *_entry(world, entry))

    text = _assert_refused(
        world,
        _UNMANAGED,
        "--root",
        str(target),
        *_entry(world, entry),
        extra=target,
        keep_git_cache=True,
    )
    assert _HAND_WRITTEN in text, text


@pytest.mark.parametrize("offending", [True, False], ids=["unmanaged-file", "control"])
def test_unmanaged_files_deny_refuses_an_install_that_would_leave_drift(
    lifecycle_world: World,  # noqa: F811
    offending: bool,
) -> None:
    world = lifecycle_world
    spec = world.packages["p-skill:git"].spec
    use_policy(world, "unmanaged-deny")
    if offending:
        _seed(world)
        path = world.project / _HAND_WRITTEN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand written\n", encoding="utf-8")

    if offending:
        text = _assert_refused(world, _UNMANAGED, spec, keep_git_cache=True)
        assert _HAND_WRITTEN in text, text
        audit = _apm(world, "audit", "--ci")
        assert audit.returncode == 1, _evidence(audit)
        assert _HAND_WRITTEN in _text(audit), _evidence(audit)
    else:
        _assert_ok(world, "install", spec)
        assert (world.project / ".agents" / "skills" / "p-skill" / "SKILL.md").is_file()


@pytest.mark.parametrize("offending", [True, False], ids=["unmanaged-file", "control"])
def test_unmanaged_files_deny_refuses_a_local_bundle_install(
    lifecycle_world: World,  # noqa: F811
    offending: bool,
) -> None:
    world = lifecycle_world
    bundle = world.packages["p-exec:bundle"].spec
    use_policy(world, "unmanaged-deny")
    _seed(world)
    if offending:
        path = world.project / _HAND_WRITTEN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand written\n", encoding="utf-8")

        text = _assert_refused(world, _UNMANAGED, bundle)
        assert _HAND_WRITTEN in text, text
    else:
        _assert_ok(world, "install", bundle)
        _assert_ok(world, "audit", "--ci")


def test_unmanaged_files_deny_refuses_an_mcp_install(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "unmanaged-deny")
    _seed(world)
    path = world.project / _HAND_WRITTEN
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# hand written\n", encoding="utf-8")

    text = _assert_refused(
        world, _UNMANAGED, "--mcp", "srv", "--url", "https://mcp.example.com/mcp"
    )
    assert _HAND_WRITTEN in text, text


def test_unmanaged_files_deny_judges_custom_directories_for_an_mcp_install(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "unmanaged-deny-dirs")
    _seed(world)
    path = world.project / "governed" / "hand-written.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# hand written\n", encoding="utf-8")

    text = _assert_refused(
        world, _UNMANAGED, "--mcp", "srv", "--url", "https://mcp.example.com/mcp"
    )
    assert "governed/hand-written.md" in text, text


@pytest.mark.parametrize("source", ["bundle", "local-content"])
def test_unmanaged_files_deny_lets_a_reinstall_adopt_files_after_lockfile_loss(
    lifecycle_world: World,  # noqa: F811
    source: str,
) -> None:
    world = lifecycle_world
    use_policy(world, "unmanaged-deny")
    _seed(world)
    if source == "bundle":
        args: tuple[str, ...] = (str(_rules_bundle(world)),)
    else:
        local = world.project / ".apm" / "instructions" / "house.instructions.md"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("---\napplyTo: '**'\n---\n# House rules\n", encoding="utf-8")
        args = ()
    _assert_ok(world, "install", *args)
    (world.project / "apm.lock.yaml").unlink()

    _assert_ok(world, "install", *args)

    name = "rules" if source == "bundle" else "house"
    lock = (world.project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert f".github/instructions/{name}.instructions.md" in lock, lock


@pytest.mark.parametrize("force", [False, True], ids=["skipped", "forced"])
def test_unmanaged_files_deny_judges_a_bundle_file_the_install_skips(
    lifecycle_world: World,  # noqa: F811
    force: bool,
) -> None:
    world = lifecycle_world
    bundle = str(_rules_bundle(world))
    use_policy(world, "unmanaged-deny")
    _seed(world)
    path = world.project / ".github" / "instructions" / "rules.instructions.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# hand written\n", encoding="utf-8")

    if force:
        _assert_ok(world, "install", "--force", bundle)
        _assert_ok(world, "audit", "--ci")
    else:
        text = _assert_refused(world, _UNMANAGED, bundle)
        assert ".github/instructions/rules.instructions.md" in text, text


@pytest.mark.parametrize("variant", ["scripts-deny", "unmanaged-deny"])
def test_mcp_dry_run_previews_the_refusal(
    lifecycle_world: World,  # noqa: F811
    variant: str,
) -> None:
    world = lifecycle_world
    use_policy(world, variant)
    _seed(world)
    if variant == "scripts-deny":
        _add_scripts(world.project / "apm.yml")
        expected = _SCRIPTS[0]
    else:
        path = world.project / _HAND_WRITTEN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand written\n", encoding="utf-8")
        expected = _UNMANAGED[0]
    before = _state(world, None, keep_git_cache=False)

    result = _assert_ok(
        world, "install", "--dry-run", "--mcp", "srv", "--url", "https://mcp.example.com/mcp"
    )

    text = _text(result)
    assert "Would be blocked by policy" in text, text
    assert expected in text, text
    assert _state(world, None, keep_git_cache=False) == before


def test_unmanaged_files_deny_keeps_files_root_local_content_deploys(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    local = world.project / ".apm" / "instructions" / "house.instructions.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("---\napplyTo: '**'\n---\n# House rules\n", encoding="utf-8")
    use_policy(world, "unmanaged-deny")

    _assert_ok(world, "install", world.packages["p-skill:git"].spec)

    assert (world.project / ".github" / "instructions" / "house.instructions.md").is_file()
    _assert_ok(world, "audit", "--ci")


def test_unmanaged_files_deny_dry_run_previews_the_refusal(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    path = world.project / _HAND_WRITTEN
    path.parent.mkdir(parents=True, exist_ok=True)
    use_policy(world, "unmanaged-deny")
    _seed(world)
    path.write_text("# hand written\n", encoding="utf-8")
    before = _state(world, None, keep_git_cache=True)

    result = _assert_ok(world, "install", "--dry-run", world.packages["p-skill:git"].spec)

    text = _text(result)
    assert "Would be blocked by policy" in text, text
    assert _UNMANAGED[0] in text, text
    assert _HAND_WRITTEN in text, text
    assert _state(world, None, keep_git_cache=True) == before


@pytest.mark.parametrize("offending", [True, False], ids=["too-deep", "control"])
def test_max_depth_refuses_a_graph_deeper_than_the_limit(
    lifecycle_world: World,  # noqa: F811
    offending: bool,
) -> None:
    world = lifecycle_world
    use_policy(world, "max-depth-1")

    if offending:
        _seed(world, package="p-skill:git")
        text = _assert_refused(world, _DEPTH, world.packages["p-parent"].spec, keep_git_cache=True)
        assert "p-child" in text, text
    else:
        _assert_ok(world, "install", world.packages["p-skill:git"].spec)


def test_max_depth_refusal_keeps_a_legacy_lockfile_in_place(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "max-depth-1")
    _seed(world, package="p-skill:git")
    (world.project / "apm.lock.yaml").rename(world.project / "apm.lock")

    _assert_refused(world, _DEPTH, world.packages["p-parent"].spec, keep_git_cache=True)
    assert not (world.project / "apm.lock.yaml").exists()


def test_max_depth_reuses_a_dependency_already_resolved_directly(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """p-child direct, then p-parent -> p-child under max_depth 1: p-child stays at depth 1."""
    world = lifecycle_world
    use_policy(world, "max-depth-1")
    _assert_ok(world, "install", world.packages["p-child"].spec)

    _assert_ok(world, "install", world.packages["p-parent"].spec)

    lock = yaml.safe_load((world.project / "apm.lock.yaml").read_text(encoding="utf-8"))
    rows = lock["dependencies"]
    assert sorted(row.get("depth", 1) for row in rows) == [1, 1], rows
    _assert_ok(world, "audit", "--ci")


def test_max_depth_terminates_on_a_cycle_without_a_false_refusal(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """c-a -> c-b -> c-a under max_depth 2: refused as a cycle, never as too deep.

    Install refuses circular dependencies by contract; the back edge reuses
    c-a at depth 1, so the depth limit never fires (the resolver row in
    test_apm_resolver_edge_cases asserts the nodes and depths).
    """
    world = lifecycle_world
    factory = LocalPackageFactory(world.root / "cycle")
    first, second = _skill_package(factory, "c-a"), _skill_package(factory, "c-b")
    factory.add_relative_dependency(first, second)
    factory.add_relative_dependency(second, first)
    use_policy(world, "max-depth-2")
    _seed(world, package="p-skill:git")
    before = _state(world, None, keep_git_cache=True)

    result = _apm(world, "install", str(first.root))

    text = _text(result)
    assert result.returncode == 1, _evidence(result)
    assert "Circular dependencies detected" in text, text
    assert _DEPTH[0] not in text, text
    assert _state(world, None, keep_git_cache=True) == before


def test_max_depth_judges_the_resolved_graph_not_the_old_lockfile(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    _assert_ok(world, "install", world.packages["p-parent"].spec)
    _assert_ok(world, "install", world.packages["p-skill:git"].spec)
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    deps = data["dependencies"]["apm"]
    data["dependencies"]["apm"] = [dep for dep in deps if "p-parent" not in str(dep)]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    use_policy(world, "max-depth-1")

    _assert_ok(world, "install")

    assert "p-child" not in (world.project / "apm.lock.yaml").read_text(encoding="utf-8")


# -- #4957: warn and --dry-run parity on every install entry, --root cleanup ---
#
# Under enforcement: warn every entry (pipeline, --mcp, local bundle) prints the
# manifest.scripts and unmanaged_files warnings the pipeline prints and exits 0;
# --dry-run previews each verdict read-only; a refused --root install leaves no
# directory it created.


def _offend(world: World) -> None:
    """A scripts: block and a governed file no lock row claims."""
    _add_scripts(world.project / "apm.yml")
    path = world.project / _HAND_WRITTEN
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# hand written\n", encoding="utf-8")


@pytest.mark.parametrize("entry", ["pipeline", "mcp", "bundle"])
def test_warn_policy_warns_on_every_install_entry(
    lifecycle_world: World,  # noqa: F811
    entry: str,
) -> None:
    world = lifecycle_world
    use_policy(world, "fields-warn")
    _seed(world)
    _offend(world)

    text = _text(_assert_ok(world, "install", *_entry(world, entry)))

    assert _SCRIPTS[0] in text, text
    assert _UNMANAGED[0] in text, text


@pytest.mark.parametrize(
    ("entry", "variant", "preview"),
    [
        ("mcp", "fields-warn", "Policy warning"),
        ("bundle", "fields-warn", "Policy warning"),
        ("bundle", "scripts-deny", "Would be blocked by policy"),
    ],
)
def test_dry_run_previews_every_entry_verdict(
    lifecycle_world: World,  # noqa: F811
    entry: str,
    variant: str,
    preview: str,
) -> None:
    world = lifecycle_world
    use_policy(world, variant)
    _seed(world)
    _add_scripts(world.project / "apm.yml")
    before = _state(world, None, keep_git_cache=False)

    text = _text(_assert_ok(world, "install", "--dry-run", *_entry(world, entry)))

    assert f"{preview}:" in text, text
    assert _SCRIPTS[0] in text, text
    assert _state(world, None, keep_git_cache=False) == before


def test_a_refused_root_install_leaves_no_directory_it_created(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "scripts-deny")
    _seed(world)
    _add_scripts(world.project / "apm.yml")
    target = world.root / "missing" / "deploy"

    _assert_refused(world, _SCRIPTS, "--root", str(target), world.packages["p-skill:git"].spec)

    assert not (world.root / "missing").exists()


def test_warn_policy_at_the_root_destination_warns_on_an_mcp_install(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """Only the --root destination carries a warn policy and holds an unmanaged file."""
    world = lifecycle_world
    target = world.root / "redirected"
    target.mkdir()
    _govern(world, target, "fields-warn")
    # --mcp records the server in the destination's apm.yml.
    (target / "apm.yml").write_bytes((world.project / "apm.yml").read_bytes())
    path = target / _HAND_WRITTEN
    path.parent.mkdir(parents=True)
    path.write_text("# hand written\n", encoding="utf-8")

    text = _text(_assert_ok(world, "install", "--root", str(target), *_MCP_ENTRY))

    assert _UNMANAGED[0] in text, text
    assert _HAND_WRITTEN in text, text
    assert path.read_text(encoding="utf-8") == "# hand written\n"
    assert (target / ".mcp.json").is_file()


def _approved_hook_bundle_install(world: World, target: str) -> None:
    """Install ``p-exec`` for *target*, approve its exact key, and install it again."""
    bundle = world.packages["p-exec:bundle"].spec
    parked = _assert_ok(world, "install", "--target", target, bundle)
    key = re.search(r"apm approve '([^']+)'", _text(parked))
    assert key is not None, _evidence(parked)
    _assert_ok(world, "approve", key.group(1))
    _assert_ok(world, "install", "--target", target, bundle)


def test_unmanaged_files_deny_passes_audit_after_an_approved_copilot_hook_bundle(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "unmanaged-deny")
    _seed(world)
    _approved_hook_bundle_install(world, "copilot")
    assert (world.project / ".github" / "hooks" / "p-exec-hooks.json").is_file()
    audit = _assert_ok(world, "audit", "--ci")
    assert "No unmanaged files in governance directories" in _text(audit), _evidence(audit)


def test_unmanaged_files_deny_counts_apm_owned_claude_hook_files(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "unmanaged-deny")
    _seed(world)
    _approved_hook_bundle_install(world, "claude")
    assert (world.project / ".claude" / "apm-hooks.json").is_file()
    audit = _apm(world, "audit", "--ci")
    assert audit.returncode == 0, _evidence(audit)


def _edit_sidecar_entry(claude: Path) -> None:
    sidecar = claude / "apm-hooks.json"
    sidecar.write_text(sidecar.read_text(encoding="utf-8").replace("echo p-exec", "echo other"))


def _drop_settings_hooks(claude: Path) -> None:
    (claude / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")


def _truncate_sidecar(claude: Path) -> None:
    sidecar = claude / "apm-hooks.json"
    sidecar.write_bytes(sidecar.read_bytes()[:-3])


def _link_sidecar(claude: Path) -> None:
    sidecar = claude / "apm-hooks.json"
    real = claude.parent / "apm-hooks.real.json"
    sidecar.replace(real)
    sidecar.symlink_to(real)


def _fifo_sidecar(claude: Path) -> None:
    sidecar = claude / "apm-hooks.json"
    sidecar.unlink()
    os.mkfifo(sidecar)


_USER_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}


def _user_hook(claude: Path, event: str) -> None:
    settings = claude / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["hooks"].setdefault(event, []).append(_USER_HOOK)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _user_hook_in_owned_event(claude: Path) -> None:
    _user_hook(claude, "SessionStart")


def _user_hook_in_another_event(claude: Path) -> None:
    _user_hook(claude, "PreToolUse")


def _forged_bundle_marker(claude: Path) -> None:
    """A hand-written hook mirrored into the sidecar under an unapproved bundle key."""
    _user_hook(claude, "PreToolUse")
    sidecar = claude / "apm-hooks.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["PreToolUse"] = [{**_USER_HOOK, "_apm_source": "local-bundle::x::y"}]
    sidecar.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# What the local-bundle-hooks check, which runs before unmanaged-files, names for a
# tamper it refuses (#5078); either refusal is fail-closed.
_LOCAL_BUNDLE_HOOK_REFUSALS = {
    _edit_sidecar_entry: "- p-exec: a deployed hook entry was changed outside APM",
    _drop_settings_hooks: "- p-exec: a deployed hook entry was changed outside APM",
    _truncate_sidecar: (
        "- .claude/settings.json: the hook config or its ownership record cannot be read"
    ),
    _forged_bundle_marker: "- x: approval no longer granted",
}


def _fingerprint(path: Path) -> object:
    """Bytes of a regular file; the file type of anything else (a FIFO is never read)."""
    mode = path.lstat().st_mode
    return path.read_bytes() if stat.S_ISREG(mode) else stat.S_IFMT(mode)


# Main's local-bundle-hooks check reads the sidecar with a blocking read (#5328).
_FIFO_SIDECAR_HANGS = KnownFailure(
    "#5328",
    "audit --ci blocks on a FIFO hook sidecar in the local-bundle-hooks check",
    r"'audit', '--ci'",
    raises=subprocess.TimeoutExpired,
)


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_edit_sidecar_entry, id="sidecar-entry-edited"),
        pytest.param(_drop_settings_hooks, id="settings-hooks-removed"),
        pytest.param(_truncate_sidecar, id="sidecar-unparsable"),
        pytest.param(_link_sidecar, id="sidecar-symlinked"),
        pytest.param(_fifo_sidecar, id="sidecar-fifo", marks=_FIFO_SIDECAR_HANGS.mark()),
        pytest.param(_user_hook_in_owned_event, id="user-hook-in-owned-event"),
        pytest.param(_user_hook_in_another_event, id="user-hook-in-another-event"),
        pytest.param(_forged_bundle_marker, id="forged-bundle-marker"),
    ],
)
def test_unmanaged_files_deny_flags_claude_hook_files_whose_ownership_does_not_verify(
    lifecycle_world: World,  # noqa: F811
    tamper,
) -> None:
    """#5277: APM's hook files count as managed only while their ownership verifies."""
    world = lifecycle_world
    use_policy(world, "unmanaged-deny")
    _seed(world)
    _approved_hook_bundle_install(world, "claude")
    claude = world.project / ".claude"
    try:
        tamper(claude)
    except (AttributeError, OSError):
        pytest.skip("symlinks or named pipes are unavailable")
    before = {name: _fingerprint(claude / name) for name in ("apm-hooks.json", "settings.json")}

    audit = _apm(world, "audit", "--ci")
    text = _text(audit)

    assert audit.returncode == 1, _evidence(audit)
    refusal = _LOCAL_BUNDLE_HOOK_REFUSALS.get(tamper)
    if refusal is not None and "local-bundle-hooks details:" in text:
        # The local-bundle-hooks check refuses the tampered files first: still fail-closed.
        assert refusal in text, _evidence(audit)
    else:
        assert "2 unmanaged file(s) in governance directories" in text, _evidence(audit)
        assert ".claude/apm-hooks.json -- not tracked in apm.lock.yaml" in text, _evidence(audit)
        assert ".claude/settings.json -- not tracked in apm.lock.yaml" in text, _evidence(audit)
    assert {name: _fingerprint(claude / name) for name in before} == before


def test_unmanaged_files_deny_still_flags_a_user_file_beside_verified_hook_files(
    lifecycle_world: World,  # noqa: F811
) -> None:
    """#5277: verified hook ownership never covers a user-authored file in the same directory."""
    world = lifecycle_world
    use_policy(world, "unmanaged-deny")
    _seed(world)
    _approved_hook_bundle_install(world, "claude")
    (world.project / ".claude" / "notes.md").write_text("# mine\n", encoding="utf-8")

    audit = _apm(world, "audit", "--ci")
    text = _text(audit)

    assert audit.returncode == 1, _evidence(audit)
    assert "1 unmanaged file(s) in governance directories" in text, _evidence(audit)
    assert ".claude/notes.md -- not tracked in apm.lock.yaml" in text, _evidence(audit)
    assert ".claude/settings.json" not in text, _evidence(audit)
    assert ".claude/apm-hooks.json" not in text, _evidence(audit)
