"""End-to-end producer journey: init -> author -> pack -> ship.

Wave 7 of microsoft/apm#1348. Exercises the full producer surface
across the four shapes APM supports:

  * single-plugin  -- one repo, one plugin (the default)
  * aggregator     -- a marketplace that vendors external plugins
  * monorepo       -- one repo, many packages under packages/*
  * hybrid         -- a plugin repo that ALSO publishes a marketplace

This file does not require external network access or tokens; it
drives the actual CLI via Click's CliRunner so every assertion runs
against the real command graph. Heavyweight network-dependent
journeys (real GitHub publish, real install from a published
marketplace) belong in tests/integration/test_*_e2e.py gated by
``APM_E2E_TESTS``.

Why per-shape rather than one mega-test: a regression in pack's
plugin emission path historically broke aggregator but not
single-plugin (see issue #1348 G2 acceptance). Splitting by shape
keeps the failure signal sharp.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def workdir():
    """Yield a kebab-safe tmp dir and restore cwd after the test."""
    original = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="apm-journey-") as tmp:
        # tmp basename contains underscores -> create a kebab-safe child
        safe = Path(tmp) / "demo-workspace"
        safe.mkdir()
        os.chdir(safe)
        try:
            yield safe
        finally:
            try:
                os.chdir(original)
            except (FileNotFoundError, OSError):
                os.chdir(str(Path(__file__).resolve().parents[2]))


# ---------------------------------------------------------------------------
# Shape 1: single-plugin
# ---------------------------------------------------------------------------


class TestSinglePluginJourney:
    """``apm plugin init`` -> author primitive -> ``apm pack``."""

    def test_init_produces_plugin_json_and_apm_yml(self, runner, workdir):
        result = runner.invoke(cli, ["plugin", "init", "single-plugin-demo", "--yes"])
        assert result.exit_code == 0, result.output
        # `apm init <name>` chdirs into the project dir
        assert Path("apm.yml").exists()
        assert Path("plugin.json").exists()

    def test_plugin_json_has_required_fields(self, runner, workdir):
        result = runner.invoke(cli, ["plugin", "init", "single-plugin-demo", "--yes"])
        assert result.exit_code == 0, result.output
        plugin_json = json.loads(Path("plugin.json").read_text())
        # Anthropic / Claude Code-style minimum schema
        assert "name" in plugin_json
        assert plugin_json["name"] == "single-plugin-demo"
        assert "version" in plugin_json

    def test_apm_yml_round_trips_through_yaml(self, runner, workdir):
        result = runner.invoke(cli, ["plugin", "init", "single-plugin-demo", "--yes"])
        assert result.exit_code == 0, result.output
        data = yaml.safe_load(Path("apm.yml").read_text())
        assert data["name"] == "single-plugin-demo"


# ---------------------------------------------------------------------------
# Shape 2: aggregator (marketplace that vendors externals)
# ---------------------------------------------------------------------------


class TestAggregatorJourney:
    """``apm init`` + ``apm marketplace init`` -> apm.yml has marketplace block."""

    def test_apm_init_then_marketplace_init(self, runner, workdir):
        # Step 1: consumer init
        r1 = runner.invoke(cli, ["init", "agg-demo", "--yes"])
        assert r1.exit_code == 0, r1.output
        # Now in agg-demo/
        assert Path("apm.yml").exists()
        # Step 2: bolt on a marketplace
        r2 = runner.invoke(cli, ["marketplace", "init"])
        assert r2.exit_code == 0, r2.output
        # Marketplace block must land in apm.yml
        content = Path("apm.yml").read_text()
        assert "marketplace:" in content

    def test_removed_init_marketplace_flag_points_at_marketplace_init(self, runner, workdir):
        """``apm init --marketplace`` was removed in 0.37 (#4073)."""
        result = runner.invoke(cli, ["init", "agg-demo", "--marketplace", "--yes"])
        assert result.exit_code == 2, result.output
        assert "Run: apm marketplace init" in result.stderr
        assert not Path("agg-demo").exists()


# ---------------------------------------------------------------------------
# Shape 3: monorepo (packages/* layout)
# ---------------------------------------------------------------------------


class TestMonorepoJourney:
    """One repo, multiple packages under packages/<name>/."""

    def test_init_two_packages_under_monorepo(self, runner, workdir):
        # Aggregator at root
        r1 = runner.invoke(cli, ["init", "monorepo-demo", "--yes"])
        assert r1.exit_code == 0, r1.output
        # Now in monorepo-demo/
        assert runner.invoke(cli, ["marketplace", "init"]).exit_code == 0
        Path("packages").mkdir(exist_ok=True)
        os.chdir("packages")
        Path("pkg-a").mkdir()
        os.chdir("pkg-a")
        r2 = runner.invoke(cli, ["plugin", "init", "--yes"])
        assert r2.exit_code == 0, r2.output
        assert Path("plugin.json").exists()
        os.chdir("..")
        Path("pkg-b").mkdir()
        os.chdir("pkg-b")
        r3 = runner.invoke(cli, ["plugin", "init", "--yes"])
        assert r3.exit_code == 0, r3.output
        assert Path("plugin.json").exists()
        # Walk back up and confirm both packages coexist
        os.chdir("../..")
        assert Path("packages/pkg-a/plugin.json").exists()
        assert Path("packages/pkg-b/plugin.json").exists()
        assert "marketplace:" in Path("apm.yml").read_text()


# ---------------------------------------------------------------------------
# Shape 4: hybrid (plugin repo that also publishes a marketplace)
# ---------------------------------------------------------------------------


class TestHybridJourney:
    """A repo that is BOTH a plugin AND publishes a marketplace."""

    def test_plugin_init_then_marketplace_init_compose_cleanly(self, runner, workdir):
        r1 = runner.invoke(cli, ["plugin", "init", "hybrid-demo", "--yes"])
        assert r1.exit_code == 0, r1.output
        # Now in hybrid-demo/
        assert Path("plugin.json").exists()
        assert Path("apm.yml").exists()
        # Add a marketplace block on top
        r2 = runner.invoke(cli, ["marketplace", "init"])
        assert r2.exit_code == 0, r2.output
        text = Path("apm.yml").read_text()
        # Both surfaces coexist
        assert "marketplace:" in text
        # plugin.json untouched
        plugin_json = json.loads(Path("plugin.json").read_text())
        assert plugin_json["name"] == "hybrid-demo"


# ---------------------------------------------------------------------------
# Cross-flow: byte-equivalence between old and new surface
# ---------------------------------------------------------------------------


class TestSurfaceEquivalence:
    """Consumer ``apm init`` teaches the producer noun-verb namespace."""

    def test_apm_init_consumer_surfaces_namespace_hints(self, runner, workdir):
        """Wave 3 v3 promise: consumer init teaches noun-verb namespace."""
        result = runner.invoke(cli, ["init", "consumer-demo", "--yes"])
        assert result.exit_code == 0, result.output
        # Both noun-verb pointers appear
        assert "apm plugin init" in result.output
        assert "apm marketplace init" in result.output
        # Consumers are pointed at install; `apm run` is no longer advertised (#4004).
        assert "apm install" in result.output


# ---------------------------------------------------------------------------
# Deprecation contract
# ---------------------------------------------------------------------------


class TestRemovedFlagContract:
    """``apm init --plugin`` / ``--marketplace`` were removed in 0.37 (#4073).

    Each stays a hidden stub: it writes nothing, exits 2 and names the
    replacement, so scripts fail loudly with the migration path.
    """

    @pytest.mark.parametrize(
        ("flag", "replacement"),
        [("--plugin", "apm plugin init"), ("--marketplace", "apm marketplace init")],
    )
    def test_removed_flag_shape(self, runner, workdir, flag, replacement):
        result = runner.invoke(cli, ["init", "x", flag, "--yes"])
        assert result.exit_code == 2
        assert f"Run: {replacement}" in result.stderr
        assert "removed in 0.37" in result.stderr
        assert not Path("x").exists()
