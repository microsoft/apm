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
* ``apm update`` verifies an unchanged registry version against its locked
  hash, while a new registry version re-locks (#4125);
* ``apm unpack`` never deploys executable content.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
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
    tree_snapshot,
    write,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)
from tests.utils.local_scripted_https_proxy import (
    ScriptedHttpsProxy,
    ScriptedRequest,
    ScriptedResponse,
    local_scripted_https_proxy,
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


_REGISTRY_HOST = "registry.corp.example"
_GUIDE = ".github/instructions/guide.instructions.md"


def _registry_archive(version: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, text in (
            ("apm.yml", f"name: widget\nversion: {version}\ndescription: fixture\n"),
            (
                ".apm/instructions/guide.instructions.md",
                f"---\napplyTo: '**'\ndescription: guide\n---\n# {body}\n",
            ),
        ):
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            package.writestr(info, text)
    return buffer.getvalue()


def _archive_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class _RegistryWorld:
    """A consumer of ``acme/widget`` from a scripted REST registry, policy on.

    PROMISE: source.ref_cache_coherent -- a registry package is its name plus
    exact version; ``apm update`` re-locks only a moved version (#4125).
    CELL: OP=update SRC=registry PRIM=instruction SCOPE=project TGT=copilot
    """

    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        self.runner = _runner(apm_binary_path)
        self.archives = {"1.0.0": _registry_archive("1.0.0", "one")}
        self.project = self.isolated.work_root / "consumer"
        write(self.project / ".github" / "copilot-instructions.md", "# c\n")
        write(
            self.project / "apm.yml",
            "name: consumer\nversion: 1.0.0\ndescription: registry consumer\n"
            "targets: [copilot]\n"
            f"registries:\n  fixture:\n    url: https://{_REGISTRY_HOST}/apm\n"
            "  default: fixture\ndependencies:\n  apm:\n    - acme/widget#^1.0.0\n",
        )

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        tail = request.path.rstrip("/").split("/v1/packages/acme/widget/", 1)[-1]
        if tail == "versions":
            entries = [
                {
                    "version": version,
                    "digest": hashlib.sha256(data).hexdigest(),
                    "published_at": "2026-01-01T00:00:00Z",
                }
                for version, data in self.archives.items()
            ]
            body = json.dumps({"versions": entries}).encode()
            return ScriptedResponse(200, {"Content-Type": "application/json"}, body)
        version = tail.removeprefix("versions/").removesuffix("/download")
        if version in self.archives:
            return ScriptedResponse(
                200, {"Content-Type": "application/zip"}, self.archives[version]
            )
        return ScriptedResponse(404)

    def serve(self):
        return local_scripted_https_proxy(self.isolated.root / "tls", {_REGISTRY_HOST: self.handle})

    def run(self, env: dict, *args: str, step: str, expected: int = 0):
        result = run_step(
            self.runner, args, cwd=self.project, env=env, step=step, expected=expected
        )
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), output(result)
            status = [line.strip() for line in stream.splitlines() if line.startswith("[")]
            assert len(status) == len(set(status)), output(result)
        return result

    def install(self, proxy: ScriptedHttpsProxy) -> dict:
        env = proxy.child_environment(self.isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        self.run(env, "experimental", "enable", "registries", step="enable")
        self.run(env, "install", step="install")
        return env

    def locked(self) -> dict:
        return load_yaml(self.project / "apm.lock.yaml")["dependencies"][0]

    def snapshot(self) -> tuple[LifecycleStateSnapshot, dict[str, str]]:
        state = LifecycleStateSnapshot.capture(self.project, targets=("copilot",))
        return state, tree_snapshot(self.project, exclude=(".git", "apm_modules"))


def _changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def test_update_refuses_altered_bytes_for_an_unchanged_registry_version(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _RegistryWorld(tmp_path, apm_binary_path)
    with world.serve() as proxy:
        env = world.install(proxy)
        locked = world.locked()
        assert locked["version"] == "1.0.0", locked
        altered = _registry_archive("1.0.0", "altered")
        world.archives["1.0.0"] = altered
        shutil.rmtree(world.project / "apm_modules")
        state_before, files_before = world.snapshot()

        result = world.run(env, "update", "--yes", step="update-same-version", expected=1)

        state_after, files_after = world.snapshot()
        assert state_after == state_before, output(result)
        assert _changed(files_before, files_after) == set(), output(result)
        text = output(result)
        flat = "".join(text.split())
        assert "[x]" in text, text
        assert "Contenthashmismatchforacme/widgetversion1.0.0" in flat, text
        assert f"expected{locked['resolved_hash']},got{_archive_hash(altered)}" in flat, text

        # Printed hint: clean the cache and retry; the same bytes still fail closed.
        world.run(env, "cache", "clean", "--yes", step="hint-cache-clean")
        world.run(env, "update", "--yes", step="hint-retry", expected=1)
        assert world.snapshot()[1] == files_after

        # Printed hint: drop the lock entry and run apm install to accept the bytes.
        lock = load_yaml(world.project / "apm.lock.yaml")
        lock["dependencies"] = []
        dump_yaml(lock, world.project / "apm.lock.yaml")
        hinted = world.run(env, "install", step="hint-install")
        accepted = world.locked()
        assert (accepted["version"], accepted["resolved_hash"]) == ("1.0.0", _archive_hash(altered))
        # The dropped entry took its deployment record, so the file is announced as skipped.
        assert "apm install --force" in output(hinted), output(hinted)
        world.run(env, "install", "--force", step="hint-install-force")
        assert "altered" in (world.project / _GUIDE).read_text(encoding="utf-8")


def test_update_accepts_a_new_registry_version(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _RegistryWorld(tmp_path, apm_binary_path)
    with world.serve() as proxy:
        env = world.install(proxy)
        locked = world.locked()

        # An unchanged version whose bytes match the lock re-downloads and is accepted.
        shutil.rmtree(world.project / "apm_modules")
        world.run(env, "cache", "clean", "--yes", step="cold-cache")
        state_before, files_before = world.snapshot()
        world.run(env, "update", "--yes", step="update-same-version-intact")
        assert world.snapshot() == (state_before, files_before)
        assert world.locked()["resolved_hash"] == locked["resolved_hash"]

        world.archives["1.1.0"] = _registry_archive("1.1.0", "two")
        state_before, files_before = world.snapshot()

        result = world.run(env, "update", "--yes", "--verbose", step="update-moved-version")

        state_after, files_after = world.snapshot()
        assert _changed(files_before, files_after) == {"apm.lock.yaml", _GUIDE}, output(result)
        assert state_after.manifest_bytes == state_before.manifest_bytes
        after = world.locked()
        assert after["version"] == "1.1.0", after
        assert after["resolved_hash"] == _archive_hash(world.archives["1.1.0"])
        assert after["content_hash"] != locked["content_hash"]
        assert "two" in (world.project / _GUIDE).read_text(encoding="utf-8")
        flat = "".join(output(result).split())
        assert "Newlockedcontentfor1dependency" in flat, output(result)
        assert after["content_hash"] in flat, output(result)

        # I2: the converged update changes no bytes and reports nothing new.
        again = world.run(env, "update", "--yes", step="update-again")
        assert world.snapshot() == (state_after, files_after)
        assert "New locked content" not in output(again), output(again)


def _packed_hooked_dependency_bundle(
    world: PolicyScopeWorld, runner: ApmLifecycleRunner, env: dict, out: Path
) -> Path:
    """Pack a ``--format apm`` bundle whose dependency deploys hooks."""
    hooked = world.packages["pkg-hooked"]
    producer = world.consumer(
        "producer",
        dependencies=[f"{hooked.slug}#v1.0.0"],
        targets=("claude",),
        extra={"executables": {"allow": {hooked.slug: {"hooks": True}}}},
    )
    (producer / ".claude").mkdir()
    run_step(runner, ("install", "--no-policy"), cwd=producer, env=env, step="producer")
    assert _hooks_deployed(producer)
    run_step(
        runner, ("pack", "--format", "apm", "-o", str(out)), cwd=producer, env=env, step="pack"
    )
    return next(out.iterdir())


def test_unpack_refuses_executable_content(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    _publish_hooked(world, "pkg-hooked")
    runner = _runner(apm_binary_path)
    target = world.isolated.work_root / "target"
    target.mkdir()
    (target / ".claude").mkdir()

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_hooked_dependency_bundle(world, runner, env, tmp_path / "out")

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


# -- apm unpack uses the shared install owners (#4080) -------------------------


def _packed_skill_bundle(
    world: PolicyScopeWorld, runner: ApmLifecycleRunner, env: dict, out: Path
) -> Path:
    """Pack a ``--format apm`` bundle whose one dependency is ``contoso/pkg-a``."""
    pkg = world.packages["pkg-a"]
    producer = world.consumer("producer", dependencies=[f"{pkg.slug}#v1.0.0"], targets=("claude",))
    (producer / ".claude").mkdir()
    run_step(runner, ("install", "--no-policy"), cwd=producer, env=env, step="producer")
    run_step(
        runner, ("pack", "--format", "apm", "-o", str(out)), cwd=producer, env=env, step="pack"
    )
    return next(out.iterdir())


def _unpack_target(world: PolicyScopeWorld, name: str = "target", **kwargs) -> Path:
    target = world.consumer(name, targets=("claude",), **kwargs)
    (target / ".claude").mkdir()
    return target


_SKILL = ".claude/skills/pkg-a-skill/SKILL.md"


def _unpack(bundle: Path, target: Path) -> tuple[str, ...]:
    # The restore contract of microsoft/apm-action: ``apm unpack <bundle> -o <dir>``.
    return ("unpack", str(bundle), "-o", str(target))


def test_unpack_applies_org_dependency_policy(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy({**_POLICY, "dependencies": {"deny": [f"{ORG}/pkg-a"]}})
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        target = _unpack_target(world)
        before = tree_snapshot(target)

        result = run_step(
            runner, _unpack(bundle, target), cwd=target, env=env, step="denied", expected=1
        )

        assert f"{ORG}/pkg-a" in _ascii_output(result), output(result)
        assert tree_snapshot(target) == before, output(result)

        # A preview reports the block, exits 0 and writes nothing.
        preview = run_step(
            runner, (*_unpack(bundle, target), "--dry-run"), cwd=target, env=env, step="preview"
        )
        assert "Would be blocked by policy" in output(preview), output(preview)
        assert tree_snapshot(target) == before, output(preview)

        # The printed escape hatch deploys the same bundle.
        hatch = {**env, "APM_POLICY_DISABLE": "1"}
        result = run_step(runner, _unpack(bundle, target), cwd=target, env=hatch, step="hatch")
        assert (target / _SKILL).is_file(), output(result)


def test_unpack_applies_org_target_policy(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy({**_POLICY, "compilation": {"target": {"allow": ["copilot"]}}})
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        target = _unpack_target(world)
        before = tree_snapshot(target)

        result = run_step(
            runner, _unpack(bundle, target), cwd=target, env=env, step="target", expected=1
        )

    text = _ascii_output(result)
    assert "[x]" in text, text
    assert "claude" in text, text
    assert tree_snapshot(target) == before, text


def test_unpack_reports_warn_policy_violations_and_deploys(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy(
        {**_POLICY, "enforcement": "warn", "dependencies": {"deny": [f"{ORG}/pkg-a"]}}
    )
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        target = _unpack_target(world)

        result = run_step(runner, _unpack(bundle, target), cwd=target, env=env, step="warned")

    text = " ".join(_ascii_output(result).split())
    assert f"denied by pattern: {ORG}/pkg-a" in text, text
    assert (target / _SKILL).is_file()


def test_unpack_refuses_altered_bytes_when_policy_requires_hashes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy({**_POLICY, "security": {"integrity": {"require_hashes": True}}})
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")

        # The intact bundle deploys and records its hashes.
        intact = _unpack_target(world, "intact")
        run_step(runner, _unpack(bundle, intact), cwd=intact, env=env, step="intact")
        assert (intact / _SKILL).read_bytes() == (bundle / _SKILL).read_bytes()
        hashes = load_yaml(intact / "apm.lock.yaml")["local_deployed_file_hashes"]
        assert set(hashes) == {_SKILL}, hashes

        (bundle / _SKILL).write_text("# altered\n", encoding="utf-8")
        target = _unpack_target(world)
        before = tree_snapshot(target)

        result = run_step(
            runner, _unpack(bundle, target), cwd=target, env=env, step="altered", expected=1
        )

    assert "[x]" in output(result), output(result)
    assert _SKILL in "".join(output(result).split()), output(result)
    assert tree_snapshot(target) == before


def test_unpack_fails_closed_when_policy_is_unavailable_under_block(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        target = _unpack_target(world, extra={"policy": {"fetch_failure_default": "block"}})
        before = tree_snapshot(target)

        result = run_step(
            runner, _unpack(bundle, target), cwd=target, env=env, step="unavailable", expected=1
        )

    text = output(result)
    assert "[x]" in text, text
    assert "fetch_failure_default=block" in text, text
    assert tree_snapshot(target) == before


_STRAY = ".github/agents/a.md"
_MAPPED = ".github/skills/pkg-a-skill/SKILL.md"


@pytest.mark.parametrize("pack_target", ["bogus", "claude", None])
def test_unpack_refuses_files_its_target_policy_cannot_place(
    tmp_path: Path, apm_binary_path: Path, pack_target: str | None
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy({**_POLICY, "compilation": {"target": {"allow": ["claude"]}}})
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        write(bundle / _STRAY, "# stray\n")
        lock = load_yaml(bundle / "apm.lock.yaml")
        lock["dependencies"][0]["deployed_files"].append(_STRAY)
        if pack_target is None:
            del lock["pack"]["target"]
        else:
            lock["pack"]["target"] = pack_target
        dump_yaml(lock, bundle / "apm.lock.yaml")
        target = _unpack_target(world)
        before = tree_snapshot(target)

        result = run_step(
            runner, _unpack(bundle, target), cwd=target, env=env, step="placed", expected=1
        )

    text = _ascii_output(result)
    assert "[x]" in text, text
    assert "Unpacked" not in text, text
    assert tree_snapshot(target) == before, text


def _write_lockfile_case(project: Path, case: str) -> None:
    lock = project / "apm.lock.yaml"
    if case == "malformed":
        lock.write_text("lockfile_version: '1'\ndependencies: [\n", encoding="utf-8")
    elif case == "newer":
        lock.write_text(
            "lockfile_version: '1'\napm_version: 999.0.0\ndependencies: []\n", encoding="utf-8"
        )
    else:
        real = project / "real.lock.yaml"
        real.write_text("lockfile_version: '1'\ndependencies: []\n", encoding="utf-8")
        lock.symlink_to(real.name)


@pytest.mark.parametrize("case", ["malformed", "symlink", "newer"])
def test_unpack_refuses_an_unwritable_project_lockfile_before_writing(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy(_POLICY)
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        project = _unpack_target(world)
        _write_lockfile_case(project, case)
        before = tree_snapshot(project)

        result = run_step(
            runner, _unpack(bundle, project), cwd=project, env=env, step=case, expected=1
        )

    text = _ascii_output(result)
    assert "[x]" in text, text
    assert "Unpacked" not in text, text
    assert tree_snapshot(project) == before, text
    assert not (project / _SKILL).exists()


def test_unpack_verifies_cross_target_mapped_files_under_require_hashes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-a")
    world.publish_policy({**_POLICY, "security": {"integrity": {"require_hashes": True}}})
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        producer = world.consumer(
            "producer", dependencies=[f"{pkg.slug}#v1.0.0"], targets=("claude",)
        )
        (producer / ".claude").mkdir()
        run_step(runner, ("install", "--no-policy"), cwd=producer, env=env, step="producer")
        assert (producer / _SKILL).is_file()
        out = tmp_path / "out"
        run_step(
            runner,
            ("pack", "--format", "apm", "--target", "copilot", "-o", str(out)),
            cwd=producer,
            env=env,
            step="pack-copilot",
        )
        bundle = next(out.iterdir())
        assert (bundle / _MAPPED).is_file()

        # The remapped file carries its locked hash and deploys.
        mapped = _unpack_target(world, "mapped")
        run_step(runner, _unpack(bundle, mapped), cwd=mapped, env=env, step="mapped")
        assert (mapped / _MAPPED).read_bytes() == (bundle / _MAPPED).read_bytes()

        # Altered bytes at the remapped path are refused.
        (bundle / _MAPPED).write_text("# altered\n", encoding="utf-8")
        altered = _unpack_target(world, "altered")
        before = tree_snapshot(altered)
        result = run_step(
            runner, _unpack(bundle, altered), cwd=altered, env=env, step="altered", expected=1
        )

    assert f"Hash mismatch: {_MAPPED}" in " ".join(output(result).split()), output(result)
    assert tree_snapshot(altered) == before


def _plain_dir(world: PolicyScopeWorld, name: str, *, project: bool) -> Path:
    """A non-git output directory, optionally an APM project root."""
    root = world.isolated.work_root / name
    (root / ".claude").mkdir(parents=True)
    if project:
        write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\ntargets: [claude]\n")
    return root


def _ascii_output(result) -> str:
    text = output(result)
    assert text.isascii(), text
    return text


def test_unpack_records_deployed_files_in_the_project_lockfile(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    kept = world.publish_package("pkg-b")
    world.publish_policy(_POLICY)
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        project = _unpack_target(world, dependencies=[f"{kept.slug}#v1.0.0"])
        run_step(runner, ("install",), cwd=project, env=env, step="install-kept")
        kept_skill = ".claude/skills/pkg-b-skill/SKILL.md"
        assert (project / kept_skill).is_file()
        locked_deps = load_yaml(project / "apm.lock.yaml")["dependencies"]
        before = tree_snapshot(project)

        result = run_step(runner, _unpack(bundle, project), cwd=project, env=env, step="unpack")

        _ascii_output(result)
        after = tree_snapshot(project)
        assert set(after) - set(before) == {_SKILL}
        assert {k for k in before if after.get(k) != before[k]} == {"apm.lock.yaml"}
        lock = load_yaml(project / "apm.lock.yaml")
        # Existing lock entries survive; the bundle files are added beside them.
        assert lock["dependencies"] == locked_deps
        assert lock["local_deployed_files"] == [_SKILL]
        assert lock["local_deployed_file_hashes"][_SKILL].startswith("sha256:")

        # Converged: unpacking again into the default output (cwd) changes no bytes.
        run_step(runner, ("unpack", str(bundle)), cwd=project, env=env, step="unpack-again")
        assert tree_snapshot(project) == after

        # A later install keeps the recorded files.
        run_step(runner, ("install",), cwd=project, env=env, step="install")
        assert (project / _SKILL).read_bytes() == (bundle / _SKILL).read_bytes()
        assert load_yaml(project / "apm.lock.yaml")["local_deployed_files"] == [_SKILL]
        assert (project / kept_skill).is_file()
        assert load_yaml(project / "apm.lock.yaml")["dependencies"] == locked_deps

        # The record is what audit reads: altered bytes are reported.
        audit = ("audit", "--ci", "--no-drift")
        run_step(runner, audit, cwd=project, env=env, step="audit-clean")
        (project / _SKILL).write_text("# altered\n", encoding="utf-8")
        result = run_step(runner, audit, cwd=project, env=env, step="audit-altered", expected=1)
    assert "content-integrity" in output(result), output(result)
    assert _SKILL in "".join(output(result).split()), output(result)


def test_unpack_into_a_project_keeps_unapproved_executables_out(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    _publish_hooked(world, "pkg-hooked")
    runner = _runner(apm_binary_path)
    project = _plain_dir(world, "project", project=True)
    before = tree_snapshot(project)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_hooked_dependency_bundle(world, runner, env, tmp_path / "out")
        result = run_step(
            runner, _unpack(bundle, project), cwd=project, env=env, step="unpack", expected=1
        )

    assert "Refusing to unpack" in output(result), output(result)
    assert tree_snapshot(project) == before
    assert not _hooks_deployed(project)


def test_unpack_into_a_non_project_dir_announces_and_writes_no_lockfile(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
    output_dir = _plain_dir(world, "restore", project=False)

    result = run_step(
        runner,
        _unpack(bundle, output_dir),
        cwd=output_dir,
        env=world.isolated.subprocess_env(),
        step="unpack",
    )

    text = " ".join(_ascii_output(result).split())
    assert "[i] unpack output is not tracked: the output directory has no apm.yml." in text
    assert set(tree_snapshot(output_dir)) == {_SKILL}

    # The printed hint: unpacking into an APM project records the files.
    write(output_dir / "apm.yml", "name: restore\nversion: 1.0.0\ntargets: [claude]\n")
    run_step(
        runner,
        _unpack(bundle, output_dir),
        cwd=output_dir,
        env=world.isolated.subprocess_env(),
        step="unpack-into-project",
    )
    assert load_yaml(output_dir / "apm.lock.yaml")["local_deployed_files"] == [_SKILL]


def test_unpack_waits_for_the_lifecycle_lock(tmp_path: Path, apm_binary_path: Path) -> None:
    import subprocess
    import time

    from filelock import FileLock

    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_package("pkg-a")
    world.publish_policy(_POLICY)
    runner = _runner(apm_binary_path)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        bundle = _packed_skill_bundle(world, runner, env, tmp_path / "out")
        target = _unpack_target(world)
        lock_path = world.isolated.home / ".apm-local" / ".apm-lifecycle.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = FileLock(str(lock_path))
        holder.acquire()
        try:
            proc = subprocess.Popen(
                [str(apm_binary_path), *_unpack(bundle, target)],
                cwd=target,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.2)
            still_waiting = proc.poll() is None
            deployed_while_held = (target / _SKILL).exists()
        finally:
            holder.release()
        text, _ = proc.communicate(timeout=300)

    assert still_waiting, text
    assert not deployed_while_held, text
    assert proc.returncode == 0, text
    assert "Another APM operation is running" in text, text
    assert (target / _SKILL).is_file(), text
