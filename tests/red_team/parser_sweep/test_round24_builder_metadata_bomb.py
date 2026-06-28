"""Round-24 red-team: marketplace builder metadata readers are an UNBOUNDED
deserialization sink reachable from ``apm pack --check-clean``.

``MarketplaceBuilder._fetch_local_metadata`` and ``_fetch_remote_metadata``
parse each package's ``apm.yml`` with stock ``yaml.safe_load`` (NOT the
round-12/13 bounded loader) and then materialise ``str(version)`` /
``description`` on the result. PyYAML's SafeLoader parses nested aliases as
shared references (cheap), but the downstream ``str(ver)`` expands a
billion-laughs / fat-alias structure exponentially -- a dump-amplification
DoS that the bounded loader's expansion-weight guard is designed to reject
at parse time.

Reachability (DEFAULT-ON command path):
    apm pack --check-clean
      -> commands/pack.py: check_marketplace_drift(drift_builder, ...)
      -> marketplace/drift_check.py:189 builder.remote_metadata_for_profile(...)
      -> builder._prefetch_metadata(resolved)
      -> builder._fetch_local_metadata(pkg) / _fetch_remote_metadata(pkg)
      -> yaml.safe_load(<untrusted package apm.yml>)  + str(version)

The package ``apm.yml`` content is untrusted: a local-path dependency
subdir on disk, or a remote dependency repo's ``apm.yml`` fetched over the
network. A malicious dependency ships a ~400-byte aliased ``version:`` and
the gate burns CPU/RAM expanding it.

SECURE outcome (post-fix, routed through yaml_io.load_yaml_str): the bomb is
rejected at parse time, ``except Exception`` swallows the YAMLError, and the
reader fails closed returning ``None`` -- never building the giant string.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.e2e


def _alias_bomb(levels: int, fan: int) -> str:
    """A pure-alias structure whose top node is bound to ``version:``.

    ``safe_load`` parses it in microseconds (shared refs), but ``str()`` on
    the top node expands to ``fan ** (levels-1)`` leaves. levels=8/fan=9
    yields ~4.7M leaves -> ~226MB string: large enough to prove the
    unbounded expansion, small enough to never risk OOM on the host.
    """
    lines = ["l0: &l0 [" + ",".join(['"x"'] * fan) + "]"]
    for i in range(1, levels):
        lines.append(f"l{i}: &l{i} [" + ",".join([f"*l{i - 1}"] * fan) + "]")
    lines.append(f"version: *l{levels - 1}")
    return "\n".join(lines) + "\n"


def _build_local_pkg_project(tmp_path):
    """Create a project root with marketplace.yml and a local package subdir
    whose apm.yml is the alias bomb. Returns (builder, pkg)."""
    from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder, ResolvedPackage

    mkt_path = tmp_path / "marketplace.yml"
    mkt_path.write_text("name: probe\n", encoding="utf-8")

    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "apm.yml").write_text(_alias_bomb(8, 9), encoding="utf-8")

    builder = MarketplaceBuilder(mkt_path, options=BuildOptions(offline=True))
    pkg = ResolvedPackage(
        name="evil",
        source_repo="",  # local-path package
        subdir="pkg",
        ref="v1.0.0",
        sha="0" * 40,
        requested_version=None,
        tags=("v1.0.0",),
        is_prerelease=False,
    )
    return builder, pkg


def test_fetch_local_metadata_rejects_alias_bomb(tmp_path):
    """The local metadata reader must fail closed on an aliased version bomb.

    At the current head it routes through stock ``yaml.safe_load`` and
    builds a multi-hundred-MB ``version`` string -> this assertion FAILS,
    demonstrating the unbounded sink. After routing through the bounded
    loader the reader returns ``None`` (YAMLError swallowed).
    """
    builder, pkg = _build_local_pkg_project(tmp_path)

    result: dict = {}

    def _run():
        result["meta"] = builder._fetch_local_metadata(pkg)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(15)
    assert not worker.is_alive(), "reader hung on alias bomb (>15s expansion)"

    meta = result.get("meta")
    # SECURE: bomb rejected -> None, or at worst a small benign value.
    if meta is not None:
        version = meta.get("version", "")
        assert len(version) < 100_000, (
            "UNBOUNDED SINK: _fetch_local_metadata expanded an aliased "
            f"version bomb to {len(version)} bytes via stock yaml.safe_load "
            "+ str(version); it must route through yaml_io.load_yaml_str "
            "and fail closed."
        )
