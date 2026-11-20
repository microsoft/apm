"""Registry URLs are validated against the spec scheme rules at parse time (#4286).

OpenAPM v0.1 req-sc-006: a ``registries.<name>.url`` with the ``http://``
scheme is a parse-time error unless the entry sets ``insecure: true`` or the
host is a loopback or RFC 1918 private address. The installed CLI applies the
rule under both manifest contracts: a refused manifest exits non-zero, names
the registry and writes nothing; an accepted one installs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, write
from tests.integration._policy_scope_kit import tree_snapshot

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_V01_SCHEMA = "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.schema.json"
_CONTRACTS = pytest.mark.parametrize(
    "schema_line", ["", f"$schema: {_V01_SCHEMA}\n"], ids=["default", "openapm-v0.1"]
)


def _world(tmp_path: Path, apm_binary_path: Path, schema_line: str, entry: str) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.apm("experimental", "enable", "registries")
    write(
        world.project / "apm.yml",
        f"{schema_line}name: proj\nversion: 1.0.0\ntargets:\n  - copilot\n"
        f"registries:\n  corp:\n{entry}",
    )
    return world


@_CONTRACTS
def test_http_registry_to_public_host_is_refused_with_nothing_written(
    tmp_path: Path, apm_binary_path: Path, schema_line: str
) -> None:
    world = _world(
        tmp_path, apm_binary_path, schema_line, "    url: http://registry.example.com/apm\n"
    )
    before = tree_snapshot(world.project)

    result = world.apm("install", NO_POLICY, expect=None)

    out = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, out
    assert "Registry 'corp' uses http:// to a non-local host" in out, out
    assert tree_snapshot(world.project) == before, out
    assert not (world.project / "apm.lock.yaml").exists(), out


@_CONTRACTS
@pytest.mark.parametrize(
    "entry",
    [
        "    url: http://127.0.0.1:9/apm\n",
        "    url: http://localhost:9/apm\n",
        "    url: http://registry.example.com/apm\n    insecure: true\n",
    ],
    ids=["loopback-ip", "localhost", "insecure-true"],
)
def test_loopback_or_insecure_http_registry_is_accepted(
    tmp_path: Path, apm_binary_path: Path, schema_line: str, entry: str
) -> None:
    world = _world(tmp_path, apm_binary_path, schema_line, entry)

    result = world.apm("install", NO_POLICY, expect=0)

    out = " ".join((result.stdout + result.stderr).split())
    assert "non-local host" not in out, out


def test_config_set_http_registry_url_to_public_host_is_refused(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    world.apm("experimental", "enable", "registries")
    config = world.isolated.config_root / "config.json"
    before = config.read_bytes()

    result = world.apm("config", "set", "registry.corp.url", "http://public.example/apm", expect=1)

    out = " ".join((result.stdout + result.stderr).split())
    assert "Registry 'corp' uses http:// to a non-local host" in out, out
    assert config.read_bytes() == before, out
    world.apm("config", "set", "registry.corp.url", "http://127.0.0.1:9/apm")
    assert "http://127.0.0.1:9/apm" in config.read_text()
