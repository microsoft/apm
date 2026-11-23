"""A plain-http registry URL is local only when its host is a strict local literal (#4521).

PROMISE: req-sc-006 -- a registry ``url`` using ``http://`` is refused unless
its host is local or the entry sets ``insecure: true``; P-INV-I7 -- the
refused command preserves every byte it found (source: deps/registry/
url_scheme.py, utils/net.py ``is_local_network_host``; CHANGELOG 0.38.0
"Breaking changes").

Integer, hex and leading-zero spellings of 10.0.0.1 are read differently by
socket resolvers, so the one owner of "local network host" accepts strict
dotted literals only. A manifest that declares such a registry fails closed
before any request; the dotted spelling and an explicit ``insecure: true``
stay accepted. The registry is declared but unused, so every accepted
control converges without the network.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("req-sc-006", "P-INV-I7"),
]

_REFUSAL = "uses http:// to a non-local host"
_HOME_CACHES = (".cache", "Library/Caches", ".apm/logs")


def _declare_registry(world: World, entry: dict[str, object]) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["registries"] = {"corp": entry}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _state(world: World) -> dict[str, str]:
    files: dict[str, str] = {}
    for label, root, skip in (
        ("project", world.project, (".git",)),
        ("home", world.home, _HOME_CACHES),
    ):
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if any(rel == s or rel.startswith(s + "/") for s in skip) or not path.is_file():
                continue
            files[f"{label}:{rel}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _said(result) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _converged(world: World) -> None:
    first = driver.run(world, ("install", world.packages["p-skill:git"].spec))
    assert first.returncode == 0, first.stdout + first.stderr
    assert (world.project / "apm.lock.yaml").is_file()


@pytest.mark.parametrize(
    "url", ["http://167772161/apm", "http://0x0a000001/apm", "http://010.0.0.1/apm"]
)
@pytest.mark.parametrize("command", [("install",), ("install", "--frozen"), ("audit", "--ci")])
def test_a_non_dotted_ipv4_http_registry_is_refused_before_any_change(
    lifecycle_world: World, url: str, command: tuple[str, ...]
) -> None:
    world = lifecycle_world
    _converged(world)
    _declare_registry(world, {"url": url})
    before = _state(world)
    world.services.reset()

    result = driver.run(world, command)

    evidence = f"\n{result.stdout}{result.stderr}"
    assert result.returncode != 0, "a non-local http:// registry was accepted" + evidence
    said = _said(result)
    assert "corp" in said and _REFUSAL in said, evidence
    assert _state(world) == before, "the refused command changed state" + evidence
    # Every spelling of the host, and its normalized form (a plain-http recorder: #5055).
    spellings = ("167772161", "0x0a000001", "010.0.0.1", "10.0.0.1")
    reached = [r for r in world.services.egress_log() if any(h in str(r) for h in spellings)]
    assert not reached, f"request toward the refused registry: {reached}" + evidence


@pytest.mark.parametrize(
    "entry",
    [
        {"url": "http://10.0.0.1/apm"},
        {"url": "http://167772161/apm", "insecure": True},
    ],
    ids=["dotted-literal", "insecure-opt-in"],
)
def test_a_dotted_literal_or_an_explicit_insecure_opt_in_stays_accepted(
    lifecycle_world: World, entry: dict[str, object]
) -> None:
    world = lifecycle_world
    _converged(world)
    _declare_registry(world, entry)

    result = driver.run(world, ("install",))

    evidence = f"\n{result.stdout}{result.stderr}"
    assert result.returncode == 0, evidence
    assert _REFUSAL not in _said(result), evidence
