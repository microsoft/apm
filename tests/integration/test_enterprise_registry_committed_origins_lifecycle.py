"""APMLifecycle: committed catalog origin for a registry-backed entry, stage (c) of #3299.

PROMISE: P-OWN-marketplace-locked-selection -- a project that commits its
catalog origin in ``apm.yml`` (``marketplaces:``) installs a registry-backed
catalog entry with no ``apm marketplace add``, locks the catalog origin and the
registry evidence, replays cold with ``install --frozen`` from the lock alone,
and refuses a frozen replay once the committed name points at another catalog
(source: docs/src/content/docs/consumer/installing-from-marketplaces.md,
"Declare marketplaces in a project").
CELL: OP=install|update SRC=marketplace-registry PRIM=instructions SCOPE=project
LOCK=absent|consistent NET=online FROZEN=on|off.

The world is stage 1's (``test_enterprise_registry_lifecycle.py``): the producer
publishes ``acme/base`` and ``acme/widget`` to the loopback Registry HTTP API,
and the company HTTPS catalog advertises ``widget`` from that registry. The
employee commits the catalog origin instead of registering it; cold CI gets only
the committed ``apm.yml`` and ``apm.lock.yaml``. Every networked step is judged
by its egress (hosts reached, the registry token only at the registry, never the
producer Git host) before its exit code, then by the lock and the deployed bytes.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_enterprise_registry_lifecycle import (
    CATALOG_HOST,
    CATALOG_URL,
    INSTALL,
    PRODUCER_GIT_HOSTS,
    REGISTRY_HOST,
    REGISTRY_NAME,
    REGISTRY_URL,
    SENTINEL,
    SENTINEL_BYTES,
    TOKEN,
    VERSION,
    EnterpriseRegistryWorld,
    _assert_nothing_deployed,
    _assert_registry_state,
    _evidence,
    _locked,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import (
    ScriptedHttpsProxy,
    local_scripted_https_proxy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-OWN-marketplace-locked-selection",
        "P-LAW-lockfile.frozen_preserves_bytes",
        "P-OWN-credential-origin-scope",
    ),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
]

CATALOG_NAME = "corp-catalog"
# A second company catalog host the committed name is later re-pointed at.
OTHER_CATALOG_HOST = "catalog2.corp.example"
OTHER_CATALOG_URL = f"https://{OTHER_CATALOG_HOST}/apm/marketplace.json"
DECLARATION = {"name": "widget", "marketplace": CATALOG_NAME}
FROZEN = (*INSTALL, "--frozen")
UPDATE = ("update", "--yes", "--target", "copilot")


class CommittedOriginWorld(EnterpriseRegistryWorld):
    """Stage 1's world, with the catalog entry registry-backed and a second catalog host."""

    def __init__(self, tmp_path: Path, binary: Path) -> None:
        super().__init__(tmp_path, binary)
        self.catalog["plugins"][0].update(registry=REGISTRY_NAME, version=VERSION)

    @contextmanager
    def serve(self) -> Iterator[ScriptedHttpsProxy]:
        handlers = {
            REGISTRY_HOST: self.registry.handle,
            CATALOG_HOST: self._catalog,
            OTHER_CATALOG_HOST: self._catalog,
        }
        handlers.update(dict.fromkeys(PRODUCER_GIT_HOSTS, self._producer_git))
        with local_scripted_https_proxy(self.tls, handlers) as proxy:
            yield proxy

    def consumer_manifest(self, catalog_url: str) -> dict[str, object]:
        """The committed project: its catalog origin, the registry map, no default route."""
        return {
            "name": "consumer",
            "version": VERSION,
            "description": "committed catalog origin fixture",
            "targets": ["copilot"],
            "registries": {REGISTRY_NAME: {"url": REGISTRY_URL}},
            "marketplaces": [{"name": CATALOG_NAME, "source": catalog_url}],
            "dependencies": {"apm": [DECLARATION]},
        }

    def step(
        self,
        proxy: ScriptedHttpsProxy,
        env: dict[str, str],
        cwd: Path,
        *args: str,
        step: str,
        hosts: frozenset[str],
        expect_failure: bool = False,
    ) -> CommandResult:
        """One networked step judged by egress first: only *hosts*, token only at the registry."""
        proxy.clear()
        result = self.run(env, cwd, *args, step=step, expect=None)
        reached = {o.host for o in proxy.observations}
        assert reached <= hosts, f"step={step} reached {sorted(reached - hosts)}"
        assert self.producer_git == [], f"step={step} contacted the producer Git host"
        leaked = proxy.hosts_receiving(TOKEN)
        assert leaked <= {REGISTRY_HOST}, f"step={step} sent the registry token to {leaked}"
        failed = result.returncode != 0
        assert failed is expect_failure, f"step={step}\n{_evidence(result, proxy)}"
        return result


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _catalog_fetches(proxy: ScriptedHttpsProxy) -> list[str]:
    return [o.path for o in proxy.observations if o.host in {CATALOG_HOST, OTHER_CATALOG_HOST}]


def _assert_catalog_provenance(project: Path, catalog_url: str) -> None:
    widget = _locked(project)["acme/widget"]
    assert widget.get("discovered_via") == CATALOG_NAME, widget
    assert widget.get("marketplace_plugin_name") == "widget", widget
    assert widget.get("source_url") == catalog_url, widget


def test_committed_catalog_origin_installs_replays_frozen_and_refuses_a_rebound_name(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = CommittedOriginWorld(tmp_path, apm_binary_path)
    with world.serve() as proxy:
        artifacts = world.publish_all(proxy)

        # Employee: the committed origin resolves the entry; nothing is registered.
        employee = world.env(world.employee, proxy)
        project = world.employee.work_root / "consumer"
        (project / SENTINEL).parent.mkdir(parents=True)
        (project / SENTINEL).write_bytes(SENTINEL_BYTES)
        dump_yaml(world.consumer_manifest(CATALOG_URL), project / "apm.yml")
        world.provision(world.employee, employee, project)
        installed = world.step(
            proxy,
            employee,
            project,
            *INSTALL,
            step="employee-install",
            hosts=frozenset({CATALOG_HOST, REGISTRY_HOST}),
        )
        assert proxy.hosts_receiving(TOKEN) == {REGISTRY_HOST}, _evidence(installed, proxy)
        assert _catalog_fetches(proxy) == ["/apm/marketplace.json"], _evidence(installed, proxy)
        assert load_yaml(project / "apm.yml")["dependencies"]["apm"] == [DECLARATION]
        _assert_registry_state(project, artifacts, world.registry)
        _assert_catalog_provenance(project, CATALOG_URL)
        assert not (world.employee.config_root / "marketplaces.json").exists(), (
            "the committed origin was written to the marketplace registry"
        )
        assert (project / SENTINEL).read_bytes() == SENTINEL_BYTES
        locked_bytes = (project / "apm.lock.yaml").read_bytes()

        # Employee update: the catalog still advertises the locked version; nothing moves.
        world.step(
            proxy,
            employee,
            project,
            *UPDATE,
            step="employee-update",
            hosts=frozenset({CATALOG_HOST, REGISTRY_HOST}),
        )
        assert (project / "apm.lock.yaml").read_bytes() == locked_bytes
        _assert_registry_state(project, artifacts, world.registry)

        # Cold CI: committed files only; the lock alone replays, the catalog is not read.
        checkout = world.ci.work_root / "checkout"
        for relative in ("apm.yml", "apm.lock.yaml", SENTINEL):
            (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project / relative, checkout / relative)
        ci = world.env(world.ci, proxy)
        world.provision(world.ci, ci, checkout)
        replay = world.step(
            proxy, ci, checkout, *FROZEN, step="ci-frozen-replay", hosts=frozenset({REGISTRY_HOST})
        )
        assert _catalog_fetches(proxy) == [], _evidence(replay, proxy)
        assert (checkout / "apm.lock.yaml").read_bytes() == locked_bytes
        _assert_registry_state(checkout, artifacts, world.registry)
        _assert_catalog_provenance(checkout, CATALOG_URL)

        # The committed name re-pointed at another catalog: frozen refuses, nothing changes.
        rebound = world.ci.work_root / "rebound"
        for relative in ("apm.lock.yaml", SENTINEL):
            (rebound / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project / relative, rebound / relative)
        dump_yaml(world.consumer_manifest(OTHER_CATALOG_URL), rebound / "apm.yml")
        refused = world.step(
            proxy,
            ci,
            rebound,
            *FROZEN,
            step="ci-frozen-rebound",
            hosts=frozenset({OTHER_CATALOG_HOST, REGISTRY_HOST}),
            expect_failure=True,
        )
        output = _output(refused)
        for named in (CATALOG_NAME, CATALOG_URL, OTHER_CATALOG_URL, "Nothing was changed"):
            assert named in output, _evidence(refused, proxy)
        assert "Drop --frozen, then run 'apm install'" in output, _evidence(refused, proxy)
        assert (rebound / "apm.lock.yaml").read_bytes() == locked_bytes
        _assert_nothing_deployed(rebound)
        assert (rebound / SENTINEL).read_bytes() == SENTINEL_BYTES

        # The printed remedy: a plain install re-resolves through the committed map.
        world.step(
            proxy,
            ci,
            rebound,
            *INSTALL,
            step="ci-rebound-remedy",
            hosts=frozenset({OTHER_CATALOG_HOST, REGISTRY_HOST}),
        )
        assert _catalog_fetches(proxy) == ["/apm/marketplace.json"]
        _assert_registry_state(rebound, artifacts, world.registry)
        _assert_catalog_provenance(rebound, OTHER_CATALOG_URL)
        assert (rebound / SENTINEL).read_bytes() == SENTINEL_BYTES
