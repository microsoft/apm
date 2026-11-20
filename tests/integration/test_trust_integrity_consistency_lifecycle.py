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
* ``apm unpack`` never deploys executable content;
* the ``apm audit --ci`` drift replay and the lifecycle script gate route the
  org policy outcome as install does (#4079).
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


_COPILOT_HOOK = ".github/hooks/hookpkg-hooks.json"


def _target_hooks_deployed(project: Path, target: str) -> bool:
    if target == "copilot":
        return (project / _COPILOT_HOOK).is_file()
    return _hooks_deployed(project)


def _bundle_hook_world(
    tmp_path: Path, apm_binary_path: Path, *, alias: str | None = None, target: str = "claude"
):
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    runner = _runner(apm_binary_path)
    env = world.isolated.subprocess_env()
    bundle = _packed_hook_bundle(world, runner, env, tmp_path / "out")
    project = world.consumer(targets=(target,))
    if target == "claude":
        (project / ".claude").mkdir()
    _allow(project, _bundle_key(bundle))
    args = ("install", str(bundle), "--no-policy", *(("--as", alias) if alias else ()))
    result = run_step(runner, args, cwd=project, env=env, step="bundle-granted")
    assert _target_hooks_deployed(project, target), output(result)
    return world, runner, env, bundle, project


def _revoke(project: Path) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, project / "apm.yml")


@pytest.mark.parametrize("target", ["claude", "copilot"])
@pytest.mark.parametrize(
    "command",
    [
        ("install", "--no-policy"),
        ("install", "--no-policy", "--frozen"),
        ("update", "--yes"),
        ("prune",),
    ],
    ids=["install", "frozen", "update", "prune"],
)
def test_revoked_bundle_grant_retires_its_hooks(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...], target: str
) -> None:
    _world, runner, env, _bundle, project = _bundle_hook_world(
        tmp_path, apm_binary_path, target=target
    )
    _revoke(project)

    result = run_step(runner, command, cwd=project, env=env, step="after-revoke")

    assert not _target_hooks_deployed(project, target), output(result)
    assert "approval no longer granted" in output(result), output(result)
    lock = load_yaml(project / "apm.lock.yaml")
    if target == "copilot" and "--frozen" not in command:
        assert _COPILOT_HOOK not in lock.get("local_deployed_files", []), lock
    if "--frozen" not in command:
        run_step(runner, ("audit", "--ci", "--no-policy"), cwd=project, env=env, step="audit")


def test_copilot_bundle_hook_without_a_record_fails_closed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    _world, runner, env, _bundle, project = _bundle_hook_world(
        tmp_path, apm_binary_path, target="copilot"
    )
    (project / ".github" / "apm-hooks.json").unlink()

    result = run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="no-record")

    assert not _target_hooks_deployed(project, "copilot"), output(result)
    assert "no approval record" in " ".join(output(result).split()), output(result)


def test_audit_flags_a_revoked_bundle_hook_before_install_retires_it(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    _world, runner, env, _bundle, project = _bundle_hook_world(tmp_path, apm_binary_path)
    run_step(runner, ("audit", "--ci", "--no-policy"), cwd=project, env=env, step="audit-ok")
    _revoke(project)

    result = run_step(
        runner, ("audit", "--ci", "--no-policy"), cwd=project, env=env, step="audit", expected=1
    )

    assert "local-bundle-hooks" in output(result), output(result)
    assert "hookpkg: approval no longer granted" in output(result), output(result)
    assert _hooks_deployed(project)
    run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="retire")
    assert not _hooks_deployed(project)
    run_step(runner, ("audit", "--ci", "--no-policy"), cwd=project, env=env, step="audit-clean")


def test_approved_bundle_hooks_survive_install_and_alias_follows_its_key(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    _world, runner, env, _bundle, project = _bundle_hook_world(
        tmp_path, apm_binary_path, alias="team-hooks"
    )
    for command in (("install", "--no-policy"), ("install", "--no-policy", "--frozen")):
        result = run_step(runner, command, cwd=project, env=env, step="approved")
        assert _hooks_deployed(project), output(result)

    _revoke(project)
    result = run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="revoked")

    assert not _hooks_deployed(project), output(result)
    assert "local bundle 'team-hooks'" in " ".join(output(result).split()), output(result)


def test_bundle_hook_without_a_recorded_key_fails_closed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    _world, runner, env, _bundle, project = _bundle_hook_world(tmp_path, apm_binary_path)
    sidecar = project / ".claude" / "apm-hooks.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    for entries in data.values():
        for entry in entries:
            entry["_apm_source"] = "local-bundle::hookpkg"
    sidecar.write_text(json.dumps(data), encoding="utf-8")

    result = run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="no-key")

    assert not _hooks_deployed(project), output(result)
    text = " ".join(output(result).split())
    assert "no approval record" in text, text
    assert "apm install <bundle>" in text, text


@pytest.mark.parametrize("target", ["claude", "copilot"])
def test_org_deny_all_retires_deployed_bundle_hooks(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(_POLICY)
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_hook_bundle(world, runner, env, tmp_path / "out")
        project = world.consumer(targets=(target,))
        if target == "claude":
            (project / ".claude").mkdir()
        _allow(project, _bundle_key(bundle))
        result = run_step(runner, ("install", str(bundle)), cwd=project, env=env, step="granted")
        assert _target_hooks_deployed(project, target), output(result)

        world.publish_policy({**_POLICY, "executables": {"deny_all": True}})
        world.clear_policy_cache()
        result = run_step(runner, ("install",), cwd=project, env=env, step="deny-all")

    assert not _target_hooks_deployed(project, target), output(result)


@pytest.mark.parametrize("target", ["claude", "copilot"])
def test_reinstalling_an_approved_bundle_records_a_missing_key(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """A project from before the approval record keeps its hooks on reinstall."""
    _world, runner, env, bundle, project = _bundle_hook_world(
        tmp_path, apm_binary_path, target=target
    )
    key = _bundle_key(bundle)
    if target == "claude":
        sidecar = project / ".claude" / "apm-hooks.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        for entries in data.values():
            for entry in entries:
                entry["_apm_source"] = "local-bundle::hookpkg"
        sidecar.write_text(json.dumps(data), encoding="utf-8")
    else:
        (project / ".github" / "apm-hooks.json").unlink()

    result = run_step(
        runner, ("install", str(bundle), "--no-policy"), cwd=project, env=env, step="reinstall"
    )

    assert _target_hooks_deployed(project, target), output(result)
    record = project / (".claude" if target == "claude" else ".github") / "apm-hooks.json"
    assert key in record.read_text(encoding="utf-8")
    run_step(runner, ("install", "--no-policy"), cwd=project, env=env, step="kept")
    assert _target_hooks_deployed(project, target)


def _age_policy_cache(world: PolicyScopeWorld, seconds: int) -> None:
    metas = list((world.isolated.cache_root / "policy_v1").rglob("*.meta.json"))
    assert metas, "expected a populated policy cache"
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["cached_at"] = meta["cached_at"] - seconds
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _serve_unparseable_policy(world: PolicyScopeWorld) -> None:
    assert world.policy_repository is not None
    write(world.policy_repository.worktree / "apm-policy.yml", "- not-a-mapping\n")
    world.repositories.commit(world.policy_repository, message="policy")


def _pin_mismatched_policy_hash(project: Path) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest["policy"] = {"hash": f"sha256:{'0' * 64}"}
    dump_yaml(manifest, project / "apm.yml")


def _stale_policy_with_fetch_failure_block(world: PolicyScopeWorld, project: Path) -> None:
    _age_policy_cache(world, 2 * 3600)
    _serve_unparseable_policy(world)


@pytest.mark.parametrize(
    ("policy", "make_blocking", "expected"),
    [
        (_POLICY, lambda _world, project: _pin_mismatched_policy_hash(project), "hash mismatch"),
        (
            {**_POLICY, "fetch_failure": "block"},
            _stale_policy_with_fetch_failure_block,
            "fetch_failure=block",
        ),
    ],
    ids=["hash-mismatch", "cached-stale-fetch-failure-block"],
)
def test_audit_ci_replay_routes_policy_outcome_like_install(
    tmp_path: Path, apm_binary_path: Path, policy: dict, make_blocking, expected: str
) -> None:
    """The ``audit --ci`` drift replay reads org policy through outcome routing."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    hooked = _publish_hooked(world, "pkg-hooked")
    world.publish_policy(policy)
    runner = _runner(apm_binary_path)
    project = world.consumer(
        dependencies=[f"{hooked.slug}#v1.0.0"],
        targets=("claude",),
        extra={"executables": {"allow": {f"{ORG}/pkg-hooked": {"hooks": True}}}},
    )
    (project / ".claude").mkdir()

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("install",), cwd=project, env=env, step="install")
        assert _hooks_deployed(project)
        run_step(runner, ("audit", "--ci"), cwd=project, env=env, step="audit-control")

        make_blocking(world, project)
        shutil.rmtree(project / "apm_modules")

        result = run_step(
            runner, ("audit", "--ci"), cwd=project, env=env, step="audit-replay", expected=1
        )
        text = output(result)
        assert "[x]" in text, text
        assert "Blocked by org policy" in " ".join(text.split()), text
        assert expected in " ".join(text.split()), text

        # Parity: install refuses the same policy outcome.
        install = run_step(
            runner, ("install",), cwd=project, env=env, step="install-parity", expected=1
        )
        assert expected in " ".join(output(install).split()), output(install)


def test_lifecycle_scripts_are_suppressed_when_org_policy_cannot_be_resolved(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A policy discovery error suppresses lifecycle scripts with a warning."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(
        {**_POLICY, "extends": f"{ORG}/.github", "executables": {"deny_all": True}}
    )
    marker = world.isolated.root / "fired.log"
    project = world.consumer(
        extra={
            "lifecycle": {
                "pre-compile": [{"type": "command", "bash": f"echo pre-compile >> '{marker}'"}]
            }
        }
    )
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        run_step(runner, ("lifecycle", "trust"), cwd=project, env=env, step="trust")
        world.clear_policy_cache()

        result = run_step(
            runner, ("compile",), cwd=project, env=env, step="compile-cycle", expected=(0, 1)
        )

        assert not marker.exists(), output(result)
        assert "org policy could not be enforced" in output(result), output(result)

        # Control: a resolvable policy without the ceiling lets the script fire.
        world.publish_policy(_POLICY)
        world.clear_policy_cache()
        result = run_step(
            runner, ("compile",), cwd=project, env=env, step="compile-control", expected=(0, 1)
        )
        assert marker.exists(), output(result)
