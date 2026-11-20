"""Trust and integrity consistency across bundle, uninstall, update and unpack (#4027).

Hermetic chains against the installed CLI:

* a local bundle install resolves org policy as a package install does, so
  a cold policy cache still applies org ``executables.deny_all`` and an
  unavailable policy under ``fetch_failure_default: block`` fails closed;
* uninstall's survivor rebuild reads trust through the same policy-outcome
  owner, so an unverifiable org policy fails closed instead of dropping the
  org layer;
* ``apm update`` on an unchanged commit refuses altered bytes and leaves the
  lock untouched, while a moved commit is accepted;
* ``apm unpack`` never deploys executable content.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import (
    ORG,
    PolicyScopeWorld,
    PublishedPackage,
    file_digest,
    lock_repo_urls,
    output,
    run_step,
    write,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(os.name == "nt", reason="posix hook scripts"),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}


def _hooked_tree(root: Path, name: str) -> None:
    write(
        root / "apm.yml",
        f"name: {name}\nversion: 1.0.0\ndescription: hooks\nlicense: MIT\n"
        "targets: [claude]\ndependencies:\n  apm: []\n",
    )
    write(
        root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md",
        f"---\nname: {name}-skill\ndescription: {name} skill\n---\n# {name}\n",
    )
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "./scripts/x.sh"}]}
            ]
        }
    }
    write(root / ".apm" / "hooks" / "hooks.json", json.dumps(hook))
    script = root / ".apm" / "hooks" / "scripts" / "x.sh"
    write(script, f"#!/bin/sh\necho {name}\n")
    script.chmod(0o755)


def _publish_hooked(world: PolicyScopeWorld, name: str) -> PublishedPackage:
    source = world.isolated.package_root / name
    _hooked_tree(source, name)
    repository = world.repositories.create(name, source_tree=source)
    commit = world.repositories.commit(repository, message=f"{name} v1.0.0")
    world.repositories.tag(repository, "v1.0.0", commit)
    published = PublishedPackage(name=name, repository=repository, commit=commit)
    world.packages[name] = published
    return published


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)


def _packed_hook_bundle(
    world: PolicyScopeWorld, runner: ApmLifecycleRunner, env: dict, out: Path, *fmt: str
) -> Path:
    producer = world.isolated.work_root / "hookpkg"
    _hooked_tree(producer, "hookpkg")
    (producer / ".claude").mkdir()
    run_step(runner, ("install", "--no-policy"), cwd=producer, env=env, step="producer-install")
    run_step(runner, ("pack", "-o", str(out), *fmt), cwd=producer, env=env, step="pack")
    return out / "hookpkg-1.0.0"


def _bundle_key(bundle: Path) -> str:
    info = detect_local_bundle(bundle)
    return local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )


def _allow(project: Path, key: str) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest["executables"] = {"allow": {key: {"hooks": True}}}
    dump_yaml(manifest, project / "apm.yml")


def _hooks_deployed(project: Path) -> bool:
    settings = project / ".claude" / "settings.json"
    return settings.exists() and "PreToolUse" in settings.read_text(encoding="utf-8")


def test_local_bundle_applies_org_policy_on_a_cold_cache(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy({**_POLICY, "executables": {"deny_all": True}})
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_hook_bundle(world, runner, env, tmp_path / "out")
        project = world.consumer(targets=("claude",))
        (project / ".claude").mkdir()
        _allow(project, _bundle_key(bundle))
        world.clear_policy_cache()

        result = run_step(
            runner, ("install", str(bundle)), cwd=project, env=env, step="bundle-cold-cache"
        )

        assert not _hooks_deployed(project), output(result)

        # Control: without the org ceiling the same approved bundle deploys hooks.
        world.publish_policy(_POLICY)
        world.clear_policy_cache()
        result = run_step(
            runner, ("install", str(bundle)), cwd=project, env=env, step="bundle-control"
        )
        assert _hooks_deployed(project), output(result)


def test_local_bundle_fails_closed_when_policy_is_unavailable_under_block(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_hook_bundle(world, runner, env, tmp_path / "out")
        project = world.consumer(
            targets=("claude",), extra={"policy": {"fetch_failure_default": "block"}}
        )
        (project / ".claude").mkdir()
        _allow(project, _bundle_key(bundle))
        before = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))

        # A dry-run previews without failing and writes nothing.
        preview = run_step(
            runner,
            ("install", str(bundle), "--dry-run"),
            cwd=project,
            env=env,
            step="bundle-unavailable-block-dry-run",
        )
        assert "[dry-run]" in output(preview), output(preview)
        assert not _hooks_deployed(project)
        assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*")) == before

        result = run_step(
            runner,
            ("install", str(bundle)),
            cwd=project,
            env=env,
            step="bundle-unavailable-block",
            expected=1,
        )

        assert "fetch_failure_default=block" in output(result)
        assert not _hooks_deployed(project)
        after = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))
        assert after == before


def test_uninstall_survivor_rebuild_fails_closed_on_unverifiable_policy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    hooked = _publish_hooked(world, "pkg-hooked")
    other = world.publish_package("pkg-b")
    world.publish_policy(_POLICY)
    runner = _runner(apm_binary_path)
    project = world.consumer(
        dependencies=[f"{hooked.slug}#v1.0.0", f"{other.slug}#v1.0.0"],
        targets=("claude",),
        extra={"executables": {"allow": {f"{ORG}/pkg-hooked": {"hooks": True}}}},
    )
    (project / ".claude").mkdir()

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="install")
        assert _hooks_deployed(project)

        manifest = load_yaml(project / "apm.yml")
        manifest["policy"] = {"hash": f"sha256:{'0' * 64}"}
        dump_yaml(manifest, project / "apm.yml")
        settings_before = file_digest(project / ".claude" / "settings.json")

        result = run_step(
            runner,
            ("uninstall", other.slug),
            cwd=project,
            env=env,
            step="uninstall-hash-mismatch",
            expected=1,
        )

        text = output(result)
        assert "hash mismatch" in text.lower(), text
        assert "integration cleanup is incomplete" in text, text
        # The requested removal completes; the survivor's hooks are neither
        # rebuilt nor re-admitted under the unverifiable policy.
        assert lock_repo_urls(project) == {hooked.slug}
        assert file_digest(project / ".claude" / "settings.json") == settings_before


def _cached_checkout(world: PolicyScopeWorld, name: str) -> list[Path]:
    return [
        path
        for path in world.isolated.cache_root.rglob("SKILL.md")
        if f"{name}-skill" in path.parent.name
    ]


def test_update_refuses_altered_bytes_for_an_unchanged_commit(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-a")
    runner = _runner(apm_binary_path)
    project = world.consumer(dependencies=[f"{pkg.slug}#v1.0.0"])

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="install")
        cached = _cached_checkout(world, "pkg-a")
        assert cached, sorted(str(p) for p in world.isolated.cache_root.rglob("*"))[:80]
        for path in cached:
            path.write_text("# altered\n", encoding="utf-8")
        shutil.rmtree(project / "apm_modules")
        lock_before = file_digest(project / "apm.lock.yaml")

        result = run_step(
            runner,
            ("update", "--yes"),
            cwd=project,
            env=env,
            step="update-same-commit",
            expected=1,
        )

        assert "Content hash mismatch" in output(result), output(result)
        assert file_digest(project / "apm.lock.yaml") == lock_before


def test_update_accepts_new_content_for_a_moved_commit(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-a")
    runner = _runner(apm_binary_path)
    project = world.consumer(dependencies=[pkg.slug])

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="install")
        before = load_yaml(project / "apm.lock.yaml")["dependencies"][0]
        skill = pkg.repository.worktree / ".apm" / "skills" / "pkg-a-skill" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "v2\n", encoding="utf-8")
        world.repositories.commit(pkg.repository, message="pkg-a v2")

        result = run_step(
            runner, ("update", "--yes", "--verbose"), cwd=project, env=env, step="update-moved"
        )

        after = load_yaml(project / "apm.lock.yaml")["dependencies"][0]
        assert after["resolved_commit"] != before["resolved_commit"]
        assert after["content_hash"] != before["content_hash"]
        text = "".join(output(result).split())
        assert "Newlockedcontentfor1dependency" in text, output(result)
        assert after["content_hash"] in text, output(result)

        # Nothing moved, so nothing is reported.
        again = run_step(runner, ("update", "--yes"), cwd=project, env=env, step="update-again")
        assert "New locked content" not in output(again), output(again)


def test_unpack_refuses_executable_content(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    hooked = _publish_hooked(world, "pkg-hooked")
    runner = _runner(apm_binary_path)
    producer = world.consumer(
        "producer",
        dependencies=[f"{hooked.slug}#v1.0.0"],
        targets=("claude",),
        extra={"executables": {"allow": {hooked.slug: {"hooks": True}}}},
    )
    (producer / ".claude").mkdir()
    target = world.isolated.work_root / "target"
    target.mkdir()
    (target / ".claude").mkdir()

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install", "--no-policy"), cwd=producer, env=env, step="producer")
        assert _hooks_deployed(producer)
        out = tmp_path / "out"
        run_step(
            runner,
            ("pack", "--format", "apm", "-o", str(out)),
            cwd=producer,
            env=env,
            step="pack",
        )
        bundle = next(out.iterdir())

        result = run_step(
            runner, ("unpack", str(bundle)), cwd=target, env=env, step="unpack", expected=1
        )

    text = output(result)
    assert "Refusing to unpack" in text, text
    # Rich may wrap the long path, so compare with all whitespace removed.
    assert "".join(f"apm install {bundle}".split()) in "".join(text.split()), text
    assert not any((target / ".claude").iterdir())
    assert [p.name for p in target.iterdir()] == [".claude"]


def _user_manifest(root: Path, grants: dict, target: str) -> None:
    dump_yaml(
        {
            "name": "user",
            "version": "0.1.0",
            "targets": [target],
            "executables": {"allow": grants},
        },
        root / "apm.yml",
    )


def _user_hooks_deployed(home: Path, target: str) -> bool:
    if target == "claude":
        return _hooks_deployed(home)
    hooks = home / ".copilot" / "hooks"
    return hooks.is_dir() and any(hooks.rglob("*"))


@pytest.mark.parametrize("target", ["claude", "copilot"])
def test_global_bundle_reads_user_scope_from_apm_home_outside_home(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    runner = _runner(apm_binary_path)
    env = world.isolated.subprocess_env()
    bundle = _packed_hook_bundle(world, runner, env, tmp_path / "out")
    key = _bundle_key(bundle)
    home = world.isolated.home
    apm_home = tmp_path / "elsewhere" / "apm-home"
    apm_home.mkdir(parents=True)
    env = {**env, "APM_HOME": str(apm_home)}
    (home / f".{target}").mkdir(exist_ok=True)
    # A grant under ~/.apm is not the user scope when APM_HOME points elsewhere.
    (home / ".apm").mkdir(exist_ok=True)
    _user_manifest(home / ".apm", {key: {"hooks": True}}, target)
    _user_manifest(apm_home, {}, target)

    result = run_step(
        runner,
        ("install", "-g", str(bundle), "--no-policy"),
        cwd=world.isolated.work_root,
        env=env,
        step="global-bundle-ungranted",
    )

    assert not _user_hooks_deployed(home, target), output(result)
    assert (apm_home / "apm.lock.yaml").is_file()
    assert not (home / ".apm" / "apm.lock.yaml").exists()
    assert not (home / "apm.lock.yaml").exists()

    # Control: the grant in APM_HOME admits the same bundle's hooks.
    _user_manifest(apm_home, {key: {"hooks": True}}, target)
    result = run_step(
        runner,
        ("install", "-g", str(bundle), "--no-policy"),
        cwd=world.isolated.work_root,
        env=env,
        step="global-bundle-granted",
    )
    assert _user_hooks_deployed(home, target), output(result)
    lock = load_yaml(apm_home / "apm.lock.yaml")
    assert any(path.endswith("/x.sh") for path in lock.get("local_deployed_files") or []), lock
