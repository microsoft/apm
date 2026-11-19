"""Installed-CLI proof that explicit refresh revalidates marketplace catalogs (#3279).

A loopback HTTPS server signed by a private CA hosts ``url`` catalogs whose
plugins live in a local Git repository reached through a URL rewrite. The
catalog changes while the consumer's cached copy is still inside its TTL.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

from .test_tls_custom_ca import _OPENSSL_EXECUTABLE, private_ca_https_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(_OPENSSL_EXECUTABLE is None, reason="openssl CLI not available"),
]

_REMOTE = "https://gitlab.example.invalid/team/skills"
_INSTALL = ("--target", "codex", "--no-policy", "--parallel-downloads", "0")
_NOT_CURRENT = "Could not verify that marketplace"


def _skill(name: str, release: str) -> str:
    return f"---\nname: {name}\ndescription: {name} fixture\n---\n# {name} {release}\n"


def _entry(name: str, ref: str) -> dict[str, object]:
    return {
        "name": name,
        "source": {"source": "git-subdir", "url": _REMOTE, "path": f"plugins/{name}", "ref": ref},
    }


@dataclass
class _Catalogs:
    """Mutable hosted catalogs with conditional-request support and request counts."""

    bodies: dict[str, object] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    requests: dict[str, list[int]] = field(default_factory=dict)

    def publish(self, name: str, plugins: list[dict[str, object]]) -> None:
        self.bodies[name] = {"name": name, "owner": {"name": "APM Test"}, "plugins": plugins}

    def count(self, name: str) -> int:
        return len(self.requests.get(name, []))

    def statuses(self, name: str) -> list[int]:
        return list(self.requests.get(name, []))

    def handler(self) -> type[BaseHTTPRequestHandler]:
        catalogs = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                name = urlparse(self.path).path.strip("/").split("/")[0]
                mode = catalogs.modes.get(name, "ok")
                if mode == "unavailable":
                    self._reply(name, 503, b"unavailable")
                    return
                if mode == "invalid":
                    self._reply(name, 200, b"<html>not a catalog</html>")
                    return
                body = json.dumps(catalogs.bodies[name]).encode()
                etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
                if self.headers.get("If-None-Match") == etag:
                    self._reply(name, 304, b"", etag=etag)
                    return
                self._reply(name, 200, body, etag=etag)

            def _reply(self, name: str, status: int, body: bytes, *, etag: str = "") -> None:
                catalogs.requests.setdefault(name, []).append(status)
                self.send_response(status)
                if etag:
                    self.send_header("ETag", etag)
                if status != 304:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if status != 304:
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        return Handler


@dataclass
class _World:
    catalogs: _Catalogs
    server: SimpleNamespace
    isolated: IsolatedApmEnvironment
    consumer: LocalPackage
    env: dict[str, str]
    runner: ApmLifecycleRunner

    def run(self, *args: str, scenario: str) -> CommandResult:
        return self.runner.run(args, scenario_id=scenario, cwd=self.consumer.root, env=self.env)

    def catalog_url(self, name: str) -> str:
        return f"https://127.0.0.1:{self.server.port}/{name}/marketplace.json"

    def deployed(self, name: str) -> str:
        return (self.consumer.root / ".agents/skills" / name / "SKILL.md").read_text("utf-8")

    def state(self) -> dict[Path, bytes]:
        """Bytes of the manifest, lockfile, deployed skills and catalog caches."""
        roots = (
            self.consumer.root / "apm.yml",
            self.consumer.root / "apm.lock.yaml",
            self.consumer.root / ".agents",
            self.isolated.config_root / "cache" / "marketplace",
        )
        snapshot: dict[Path, bytes] = {}
        for root in roots:
            files = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in files:
                if path.is_file() and not path.name.endswith(".meta.json"):
                    snapshot[path] = path.read_bytes()
        return snapshot


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nrc={result.returncode}\n{result.stdout}\n{result.stderr}"


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


@pytest.fixture
def world(tmp_path: Path, apm_binary_path: Path) -> Iterator[_World]:
    catalogs = _Catalogs()
    with private_ca_https_server(tmp_path / "ca", handler=catalogs.handler()) as server:
        isolated = IsolatedApmEnvironment.create(
            tmp_path.resolve() / "isolated", base_env=dict(os.environ)
        )
        repositories = LocalGitRepositoryFactory(
            isolated.repository_root, env=isolated.subprocess_env()
        )
        repository = repositories.create("skills")
        packages = LocalPackageFactory(repository.worktree / "plugins")
        plugins = {name: packages.create(name, targets=("codex",)) for name in ("foo", "bar")}
        for release in ("v1", "v2"):
            for name, package in plugins.items():
                packages.add_skill(package, name, _skill(name, release))
            commit = repositories.commit(repository, message=f"release {release}")
            repositories.tag(repository, release, commit)
        env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
        env["APM_EXTRA_CA_BUNDLE"] = server.ca_path
        env["APM_TEST_LOOPBACK_PORTS"] = str(server.port)
        consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("codex",))
        runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
        yield _World(catalogs, server, isolated, consumer, env, runner)


def _register(world: _World, *names: str) -> None:
    for name in names:
        world.catalogs.publish(name, [_entry("foo", "v1"), _entry("bar", "v1")])
        added = world.run(
            "marketplace",
            "add",
            world.catalog_url(name),
            "--name",
            name,
            scenario=f"register-{name}",
        )
        assert added.returncode == 0, _evidence(added)


def _declare_from_catalog(world: _World, *names: str) -> None:
    """Declare object-form ``name`` + ``marketplace`` dependencies in apm.yml."""
    manifest = world.consumer.manifest_path
    entries = "".join(f"    - name: {name}\n      marketplace: acme\n" for name in names)
    manifest.write_text(
        manifest.read_text("utf-8").rstrip("\n") + "\ndependencies:\n  apm:\n" + entries,
        encoding="utf-8",
    )


def test_refresh_rejects_removed_entry_that_the_cache_still_lists(world: _World) -> None:
    """Positional route: a fresh-but-outdated cache no longer answers --refresh."""
    _register(world, "acme")
    seeded = world.run("install", "foo@acme", "bar@acme", *_INSTALL, scenario="seed")
    assert seeded.returncode == 0, _evidence(seeded)
    assert world.catalogs.count("acme") == 1

    world.catalogs.publish("acme", [_entry("bar", "v1")])
    cached = world.run("install", "foo@acme", *_INSTALL, scenario="ordinary-cached")
    assert cached.returncode == 0, _evidence(cached)
    assert world.catalogs.count("acme") == 1, "ordinary install must keep the cached catalog"

    # Two selections from one source share one revalidated answer.
    both = world.run(
        "install", "--refresh", "foo@acme", "bar@acme", *_INSTALL, scenario="refresh-both"
    )
    assert "Plugin 'foo' not found in marketplace 'acme'" in _output(both), _evidence(both)
    assert "bar#v1 (already in apm.yml)" in _output(both)
    assert world.catalogs.statuses("acme") == [200, 200]

    before = world.state()
    alone = world.run("install", "--refresh", "foo@acme", *_INSTALL, scenario="refresh-removed")
    assert alone.returncode == 1, _evidence(alone)
    assert "Plugin 'foo' not found in marketplace 'acme'" in _output(alone)
    assert world.state() == before
    assert world.catalogs.statuses("acme") == [200, 200, 304]


def test_update_follows_changed_pins_and_respects_selection(world: _World) -> None:
    """Object-form route: apm update resolves a re-pinned entry; selection limits scope."""
    _register(world, "acme")
    _declare_from_catalog(world, "foo", "bar")
    seeded = world.run("install", *_INSTALL, scenario="object-seed")
    assert seeded.returncode == 0, _evidence(seeded)
    assert world.deployed("foo") == _skill("foo", "v1")
    assert world.catalogs.statuses("acme") == [200], "registration already cached the catalog"

    world.catalogs.publish("acme", [_entry("foo", "v2"), _entry("bar", "v2")])
    plain = world.run("install", *_INSTALL, scenario="object-plain")
    assert plain.returncode == 0, _evidence(plain)
    assert world.catalogs.statuses("acme") == [200]
    assert world.deployed("foo") == _skill("foo", "v1")

    selected = world.run("update", "foo@acme", "--yes", "--target", "codex", scenario="update-foo")
    assert selected.returncode == 0, _evidence(selected)
    assert world.catalogs.statuses("acme") == [200, 200]
    assert world.deployed("foo") == _skill("foo", "v2")
    assert world.deployed("bar") == _skill("bar", "v1")

    everything = world.run("update", "--yes", "--target", "codex", scenario="update-all")
    assert everything.returncode == 0, _evidence(everything)
    assert world.catalogs.statuses("acme") == [200, 200, 304]
    assert world.deployed("bar") == _skill("bar", "v2")


@pytest.mark.parametrize("mode", ["unavailable", "invalid"])
def test_refresh_fails_closed_and_preserves_state(world: _World, mode: str) -> None:
    """An unverifiable catalog fails --refresh and update without touching any bytes."""
    _register(world, "acme")
    _declare_from_catalog(world, "foo")
    seeded = world.run("install", *_INSTALL, scenario="seed")
    assert seeded.returncode == 0, _evidence(seeded)
    world.catalogs.modes["acme"] = mode
    before = world.state()

    results = world.runner.run_sequence(
        (
            ("install", "--refresh", *_INSTALL),
            ("install", "--refresh", "bar@acme", *_INSTALL),
            ("update", "--yes", "--target", "codex"),
        ),
        expected_returncodes=(1, 1, 1),
        scenario_id=f"refresh-{mode}",
        cwd=world.consumer.root,
        env=world.env,
    )
    for result in results:
        assert _NOT_CURRENT in _output(result), _evidence(result)
        assert "not found in marketplace" not in _output(result)
    assert world.state() == before

    # The retained cache still serves ordinary installs.
    ordinary = world.run("install", *_INSTALL, scenario="ordinary-after-failure")
    assert ordinary.returncode == 0, _evidence(ordinary)
    assert world.deployed("foo") == _skill("foo", "v1")


def test_frozen_replay_needs_no_live_catalog(world: _World) -> None:
    """A locked restore never acquires a catalog request, even when it is down."""
    _register(world, "acme")
    seeded = world.run("install", "foo@acme", *_INSTALL, scenario="seed")
    assert seeded.returncode == 0, _evidence(seeded)
    world.catalogs.modes["acme"] = "unavailable"
    requests = world.catalogs.count("acme")

    frozen = world.run("install", "--frozen", *_INSTALL, scenario="frozen")
    assert frozen.returncode == 0, _evidence(frozen)
    assert world.catalogs.count("acme") == requests


def test_marketplace_update_reports_partial_failure(world: _World) -> None:
    """Named and all-catalog refreshes revalidate, keep the last valid cache and exit nonzero."""
    _register(world, "acme", "beta")
    cache_root = world.isolated.config_root / "cache" / "marketplace"

    named = world.run("marketplace", "update", "acme", scenario="named-304")
    assert named.returncode == 0, _evidence(named)
    assert world.catalogs.statuses("acme") == [200, 304]

    world.catalogs.publish("acme", [_entry("bar", "v1")])
    world.catalogs.modes["beta"] = "unavailable"
    beta_cache = {p: p.read_bytes() for p in cache_root.glob("*.json") if "meta" not in p.name}
    batch = world.run("marketplace", "update", scenario="all-partial")
    assert batch.returncode == 1, _evidence(batch)
    output = _output(batch)
    assert "acme (1 plugins)" in output
    assert "failed: beta" in output
    assert "Marketplace cache refreshed" not in output

    failed_named = world.run("marketplace", "update", "beta", scenario="named-failure")
    assert failed_named.returncode == 1, _evidence(failed_named)
    assert _NOT_CURRENT in _output(failed_named)
    acme_cache = [p for p in beta_cache if json.loads(p.read_text("utf-8"))["name"] == "acme"]
    assert len(acme_cache) == 1
    assert json.loads(acme_cache[0].read_text("utf-8"))["plugins"] == [_entry("bar", "v1")]
    for path, data in beta_cache.items():
        if path not in acme_cache:
            assert path.read_bytes() == data

    browse = world.run("marketplace", "browse", "beta", scenario="browse-fallback")
    assert browse.returncode == 0, _evidence(browse)
    assert "foo@beta" in browse.stdout
