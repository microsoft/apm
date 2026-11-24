"""APMLifecycle: enterprise registry marketplace lifecycle, stage 1 (#3299).

Drives the installed CLI through the enterprise path against an APM-controlled
loopback implementation of the documented Registry HTTP API
(``tests/utils/local_package_registry.py``); no vendor service, account or SDK
is involved. Three isolated environments play the roles:

* producer -- enables registries, publishes a package's required registry
  dependency first, then the package itself;
* employee -- installs the package directly from the registry with the
  registry URL mapping provisioned, then uninstalls it next to an unrelated
  file that must survive, and registers and browses a public HTTPS catalog;
* cold CI -- replays the committed ``apm.yml`` and ``apm.lock.yaml`` with
  ``install --frozen`` while the producer's Git host is unreachable, fails
  closed on an artifact outage and on wrong bytes, then passes ``audit --ci``.

Every step asserts the exit code, the resolved source and version, the
deployed bytes and the lockfile state. Later stages of #3299 land with their
owning issues and are deliberately not represented here.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import sys
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_package_registry import (
    LocalPackageRegistry,
    RegistryArtifact,
    flat_registry_archive,
)
from tests.utils.local_scripted_https_proxy import (
    ScriptedHttpsProxy,
    ScriptedRequest,
    ScriptedResponse,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "req-rg-001",
        "P-LAW-lockfile.frozen_preserves_bytes",
        "P-OWN-credential-origin-scope",
    ),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

REGISTRY_HOST = "registry.corp.example"
BASE = "/apm"
REGISTRY_URL = f"https://{REGISTRY_HOST}{BASE}"
REGISTRY_NAME = "corp"
TOKEN = "enterprise-registry-sentinel-3299"
CATALOG_HOST = "catalog.corp.example"
CATALOG_URL = f"https://{CATALOG_HOST}/apm/marketplace.json"
# Shorthand identities resolve to github.com when they are not registry-routed.
PRODUCER_GIT_HOSTS = ("github.com", "api.github.com", "codeload.github.com")
VERSION = "1.0.0"
# Dependency first: the producer publishes ``base`` before ``widget`` needs it.
PACKAGES = (("base", ()), ("widget", (f"acme/base#{VERSION}",)))
INSTALL = ("install", "--target", "copilot", "--no-policy")
SENTINEL = ".github/instructions/team.instructions.md"
SENTINEL_BYTES = b"---\napplyTo: '**'\n---\n# Team rules owned by the consumer\n"


def _instruction(name: str) -> str:
    return f"---\napplyTo: '**'\ndescription: {name} registry package\n---\n# {name}\n"


def _deployed(name: str) -> str:
    return f".github/instructions/{name}.instructions.md"


def _member(archive: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        return package.read(f".apm/instructions/{name}.instructions.md")


def _evidence(result: CommandResult, proxy: ScriptedHttpsProxy | None = None) -> str:
    text = (
        f"command={result.command[1:]!r} rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    if proxy is not None:
        seen = [(o.host, o.method, o.path, o.status) for o in proxy.observations]
        text += f"\nobservations={seen!r}"
    return text


class EnterpriseRegistryWorld:
    """Producer, employee and cold-CI environments around one loopback registry."""

    def __init__(self, tmp_path: Path, binary: Path) -> None:
        self.registry = LocalPackageRegistry(BASE, bearer_token=TOKEN)
        self.runner = ApmLifecycleRunner((str(binary),), timeout_seconds=120)
        base_env = dict(os.environ)
        self.producer = IsolatedApmEnvironment.create(tmp_path / "producer", base_env=base_env)
        self.employee = IsolatedApmEnvironment.create(tmp_path / "employee", base_env=base_env)
        self.ci = IsolatedApmEnvironment.create(tmp_path / "cold-ci", base_env=base_env)
        self.tls = tmp_path / "tls"
        self.producer_git: list[ScriptedRequest] = []
        self.extra_hosts: dict[str, Callable[[ScriptedRequest], ScriptedResponse]] = {}
        self.catalog = {
            "name": "corp-catalog",
            "owner": {"name": "Corp Platform"},
            "plugins": [
                {
                    "name": "widget",
                    "description": "Widget registry package",
                    "source": {"source": "github", "repo": "acme/widget"},
                }
            ],
        }

    def _producer_git(self, request: ScriptedRequest) -> ScriptedResponse:
        self.producer_git.append(request)
        return ScriptedResponse(503, {"Content-Type": "text/plain"}, b"producer Git unreachable")

    def _catalog(self, request: ScriptedRequest) -> ScriptedResponse:
        if request.method == "GET" and request.path == "/apm/marketplace.json":
            body = json.dumps(self.catalog).encode()
            return ScriptedResponse(200, {"Content-Type": "application/json"}, body)
        return ScriptedResponse(404)

    @contextmanager
    def serve(self) -> Iterator[ScriptedHttpsProxy]:
        handlers = {REGISTRY_HOST: self.registry.handle, CATALOG_HOST: self._catalog}
        handlers.update(self.extra_hosts)
        handlers.update(dict.fromkeys(PRODUCER_GIT_HOSTS, self._producer_git))
        with local_scripted_https_proxy(self.tls, handlers) as proxy:
            yield proxy

    def env(self, isolated: IsolatedApmEnvironment, proxy: ScriptedHttpsProxy) -> dict[str, str]:
        env = proxy.child_environment(isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"
        # Test mode keeps the CLI's own update check off the producer Git host.
        env["APM_E2E_TESTS"] = "1"
        env["COLUMNS"] = "400"
        # Let git reach HTTPS so any producer-Git fallback lands on the recorder.
        env["GIT_ALLOW_PROTOCOL"] = "file:https"
        env[f"APM_REGISTRY_TOKEN_{REGISTRY_NAME.upper()}"] = TOKEN
        return env

    def run(
        self, env: dict[str, str], cwd: Path, *args: str, step: str, expect: int | None = 0
    ) -> CommandResult:
        result = self.runner.run(args, scenario_id=step, cwd=cwd, env=env)
        if expect is not None:
            assert result.returncode == expect, f"step={step}\n{_evidence(result)}"
        return result

    def egress_step(
        self,
        proxy: ScriptedHttpsProxy,
        env: dict[str, str],
        cwd: Path,
        *args: str,
        step: str,
        allowed: str = REGISTRY_HOST,
        expect_failure: bool = False,
        token_hosts: frozenset[str] | None = None,
    ) -> CommandResult:
        """Run one networked step; egress is judged before the exit code."""
        proxy.clear()
        result = self.run(env, cwd, *args, step=step, expect=None)
        foreign = [(o.host, o.method, o.path) for o in proxy.observations if o.host != allowed]
        assert foreign == [], f"step={step} reached hosts other than {allowed}: {foreign}"
        assert self.producer_git == [], f"step={step} contacted the producer Git host"
        if token_hosts is not None:
            leaked = proxy.hosts_receiving(TOKEN)
            assert leaked == token_hosts, f"step={step} sent the registry token to {leaked}"
        failed = result.returncode != 0
        assert failed is expect_failure, f"step={step}\n{_evidence(result, proxy)}"
        return result

    def provision(self, isolated: IsolatedApmEnvironment, env: dict[str, str], cwd: Path) -> None:
        """Enable registries and provision the registry URL mapping in user config."""
        self.run(env, cwd, "experimental", "enable", "registries", step="enable-registries")
        self.run(
            env,
            cwd,
            "config",
            "set",
            f"registry.{REGISTRY_NAME}.url",
            REGISTRY_URL,
            step="provision-url-mapping",
        )
        config = json.loads((isolated.config_root / "config.json").read_text(encoding="utf-8"))
        assert config["experimental"]["registries"] is True, config
        assert config["registries"][REGISTRY_NAME]["url"] == REGISTRY_URL, config

    def manifest(self, name: str, dependencies: tuple[str, ...] = ()) -> dict[str, object]:
        manifest: dict[str, object] = {
            "name": name,
            "version": VERSION,
            "description": f"{name} enterprise registry fixture",
            "registries": {REGISTRY_NAME: {"url": REGISTRY_URL}, "default": REGISTRY_NAME},
        }
        if dependencies:
            manifest["dependencies"] = {"apm": list(dependencies)}
        return manifest

    def publish_all(self, proxy: ScriptedHttpsProxy) -> dict[str, RegistryArtifact]:
        env = self.env(self.producer, proxy)
        self.provision(self.producer, env, self.producer.work_root)
        artifacts: dict[str, RegistryArtifact] = {}
        for name, dependencies in PACKAGES:
            root = self.producer.work_root / name
            (root / ".apm" / "instructions").mkdir(parents=True)
            dump_yaml(self.manifest(name, dependencies), root / "apm.yml")
            source = root / ".apm" / "instructions" / f"{name}.instructions.md"
            source.write_text(_instruction(name), encoding="utf-8")
            (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
            self.egress_step(
                proxy,
                env,
                root,
                "publish",
                "--package",
                f"acme/{name}",
                "--registry",
                REGISTRY_NAME,
                step=f"producer-publish-{name}",
            )
            artifact = self.registry.uploads[-1]
            assert (artifact.package, artifact.version) == (f"acme/{name}", VERSION)
            assert _member(artifact.data, name) == source.read_bytes()
            artifacts[name] = artifact
        assert [a.package for a in self.registry.uploads] == ["acme/base", "acme/widget"]
        return artifacts


def _locked(project: Path) -> dict[str, dict[str, object]]:
    lock_path = project / "apm.lock.yaml"
    if not lock_path.exists():
        return {}
    return {entry["repo_url"]: entry for entry in load_yaml(lock_path).get("dependencies") or []}


def _assert_registry_state(
    project: Path, artifacts: dict[str, RegistryArtifact], registry: LocalPackageRegistry
) -> None:
    """Resolved source, version, lockfile pins and deployed bytes for every package."""
    locked = _locked(project)
    assert set(locked) == {"acme/base", "acme/widget"}, locked
    for name, artifact in artifacts.items():
        entry = locked[f"acme/{name}"]
        assert entry["source"] == "registry", entry
        assert entry["version"] == VERSION, entry
        assert entry["resolved_url"] == (
            f"https://{REGISTRY_HOST}{registry.download_path(artifact.package, VERSION)}"
        ), entry
        assert entry["resolved_hash"] == artifact.digest, entry
        assert entry["deployed_files"] == [_deployed(name)], entry
        assert (project / _deployed(name)).read_bytes() == _member(artifact.data, name)
    assert locked["acme/base"].get("resolved_by") == "acme/widget"


def _assert_nothing_deployed(project: Path) -> None:
    for name, _ in PACKAGES:
        assert not (project / _deployed(name)).exists(), name
        assert not (project / "apm_modules" / "acme" / name / "apm.yml").exists(), name


def test_enterprise_registry_publish_install_frozen_replay_audit_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = EnterpriseRegistryWorld(tmp_path, apm_binary_path)
    with world.serve() as proxy:
        artifacts = world.publish_all(proxy)

        # Employee: direct registry install with the URL mapping provisioned.
        employee = world.env(world.employee, proxy)
        project = world.employee.work_root / "consumer"
        (project / SENTINEL).parent.mkdir(parents=True)
        (project / SENTINEL).write_bytes(SENTINEL_BYTES)
        consumer = world.manifest("consumer")
        consumer["targets"] = ["copilot"]
        dump_yaml(consumer, project / "apm.yml")
        world.provision(world.employee, employee, project)
        world.egress_step(
            proxy, employee, project, *INSTALL, f"acme/widget#{VERSION}", step="employee-install"
        )
        assert proxy.hosts_receiving(TOKEN) == {REGISTRY_HOST}
        assert load_yaml(project / "apm.yml")["dependencies"]["apm"] == [f"acme/widget#{VERSION}"]
        _assert_registry_state(project, artifacts, world.registry)
        assert (project / SENTINEL).read_bytes() == SENTINEL_BYTES

        # Cold CI: committed files only, empty cache, producer Git unreachable.
        checkout = world.ci.work_root / "checkout"
        for relative in ("apm.yml", "apm.lock.yaml", SENTINEL):
            (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project / relative, checkout / relative)
        committed_lock = (checkout / "apm.lock.yaml").read_bytes()
        ci = world.env(world.ci, proxy)
        world.provision(world.ci, ci, checkout)
        frozen = (*INSTALL, "--frozen")
        widget_download = world.registry.download_path("acme/widget", VERSION)

        # --verbose surfaces which guard rejected the artifact (archive hash, not tree hash).
        for fault, reason in (
            ("outage", "registry HTTP 503"),
            ("wrong-bytes", "archive sha256 mismatch"),
        ):
            world.registry.download_fault = fault
            if fault == "wrong-bytes":
                world.registry.substitute_archive = flat_registry_archive(
                    (
                        ("apm.yml", f"name: widget\nversion: {VERSION}\n"),
                        (".apm/instructions/widget.instructions.md", "# substituted\n"),
                    )
                )
            failed = world.egress_step(
                proxy,
                ci,
                checkout,
                *frozen,
                "--verbose",
                step=f"ci-frozen-{fault}",
                expect_failure=True,
            )
            assert reason in " ".join((failed.stdout + failed.stderr).split()), _evidence(failed)
            fetched = [o for o in proxy.requests_to(REGISTRY_HOST) if o.path == widget_download]
            assert fetched, _evidence(failed, proxy)
            assert (checkout / "apm.lock.yaml").read_bytes() == committed_lock
            _assert_nothing_deployed(checkout)
        world.registry.download_fault = None

        world.egress_step(proxy, ci, checkout, *frozen, step="ci-frozen-replay")
        downloads = sorted(o.path for o in proxy.requests_to(REGISTRY_HOST))
        assert downloads == sorted(
            world.registry.download_path(f"acme/{name}", VERSION) for name, _ in PACKAGES
        ), downloads
        assert (checkout / "apm.lock.yaml").read_bytes() == committed_lock
        _assert_registry_state(checkout, artifacts, world.registry)
        assert (checkout / SENTINEL).read_bytes() == SENTINEL_BYTES

        audit = world.egress_step(
            proxy, ci, checkout, "audit", "--ci", "-f", "json", step="ci-audit"
        )
        checks = {c["name"]: c["passed"] for c in json.loads(audit.stdout)["checks"]}
        assert checks and all(checks.values()), _evidence(audit)
        assert {"ref-consistency", "deployed-files-present", "content-integrity", "drift"} <= set(
            checks
        )
        assert (checkout / "apm.lock.yaml").read_bytes() == committed_lock
        _assert_registry_state(checkout, artifacts, world.registry)

        # Employee uninstall removes both packages and keeps the unrelated file.
        world.egress_step(proxy, employee, project, "uninstall", "acme/widget", step="uninstall")
    assert _locked(project) == {}
    assert "dependencies" not in load_yaml(project / "apm.yml") or not (
        load_yaml(project / "apm.yml")["dependencies"].get("apm")
    )
    _assert_nothing_deployed(project)
    assert (project / SENTINEL).is_file(), "uninstall deleted the unrelated sentinel file"
    assert (project / SENTINEL).read_bytes() == SENTINEL_BYTES


def test_employee_registers_and_browses_public_https_catalog(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = EnterpriseRegistryWorld(tmp_path, apm_binary_path)
    project = world.employee.work_root / "consumer"
    project.mkdir()
    with world.serve() as proxy:
        env = world.env(world.employee, proxy)
        world.provision(world.employee, env, project)
        catalog_steps = {
            "add": ("marketplace", "add", CATALOG_URL),
            "browse": ("marketplace", "browse", "corp-catalog"),
        }
        results = {}
        for step, args in catalog_steps.items():
            results[step] = world.egress_step(
                proxy, env, project, *args, step=step, allowed=CATALOG_HOST, token_hosts=frozenset()
            )
            fetched = [o.path for o in proxy.requests_to(CATALOG_HOST)]
            assert fetched == ["/apm/marketplace.json"], f"step={step} fetched {fetched}"
        added, browse = results["add"], results["browse"]
    assert "corp-catalog" in added.stdout, _evidence(added)
    listing = " ".join(browse.stdout.split())
    assert "widget" in listing and "Widget registry package" in listing, _evidence(browse)
    assert "widget@corp-catalog" in listing, _evidence(browse)
    registered = json.loads((world.employee.config_root / "marketplaces.json").read_text("utf-8"))
    assert registered["marketplaces"] == [{"name": "corp-catalog", "url": CATALOG_URL, "path": ""}]
    assert not (project / "apm.yml").exists()
    assert not (project / "apm.lock.yaml").exists()


def test_origin_restore_hint_runs_as_printed(tmp_path: Path, apm_binary_path: Path) -> None:
    """After a consented alias rebind, the printed restore command runs as
    printed and ``apm update`` then proceeds on the locked package (#5264)."""
    world = EnterpriseRegistryWorld(tmp_path, apm_binary_path)
    world.catalog["plugins"][0].update(registry=REGISTRY_NAME, version=VERSION)
    partner_host = "catalog.partner.example"
    partner_url = f"https://{partner_host}/apm/marketplace.json"
    world.extra_hosts[partner_host] = world._catalog
    declaration = {"name": "widget", "marketplace": "corp-catalog"}
    with world.serve() as proxy:
        artifacts = world.publish_all(proxy)
        env = world.env(world.employee, proxy)
        project = world.employee.work_root / "consumer"
        project.mkdir()
        consumer = world.manifest("consumer")
        consumer["registries"] = {REGISTRY_NAME: {"url": REGISTRY_URL}}
        consumer["targets"] = ["copilot"]
        consumer["dependencies"] = {"apm": [declaration]}
        dump_yaml(consumer, project / "apm.yml")
        world.provision(world.employee, env, project)
        world.run(env, project, "marketplace", "add", CATALOG_URL, step="add")
        world.run(env, project, *INSTALL, step="install")
        locked = (project / "apm.lock.yaml").read_bytes()
        rebind = ("marketplace", "add", partner_url, "--name", "corp-catalog", "--yes")
        world.run(env, project, *rebind, step="rebind")

        refused = world.run(env, project, "update", "--yes", step="update-refused", expect=1)
        text = " ".join((refused.stdout + refused.stderr).split())
        restore = re.search(r"Otherwise, (.*?) to keep the locked marketplace", text)
        assert restore is not None, _evidence(refused)
        commands = re.findall(r"run '(apm [^']+)'", restore.group(1))
        assert commands and commands[-1].startswith("apm marketplace add "), commands
        assert (project / "apm.lock.yaml").read_bytes() == locked

        for number, command in enumerate(commands):
            world.run(env, project, *shlex.split(command)[1:], step=f"restore-{number}")
        world.run(env, project, "update", "--yes", step="update-after-restore")
    registered = json.loads((world.employee.config_root / "marketplaces.json").read_text("utf-8"))
    assert [(m["name"], m["url"]) for m in registered["marketplaces"]] == [
        ("corp-catalog", CATALOG_URL)
    ]
    entry = _locked(project)["acme/widget"]
    assert entry["source"] == "registry" and entry["version"] == VERSION, entry
    assert (project / _deployed("widget")).read_bytes() == _member(
        artifacts["widget"].data, "widget"
    )
