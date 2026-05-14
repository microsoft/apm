"""Unit tests for ``apm_cli.core.build_orchestrator``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apm_cli.core.build_orchestrator import (
    ArtifactProducer,
    BuildError,
    BuildOptions,
    BuildOrchestrator,
    BuildResult,  # noqa: F401
    MarketplaceProducer,
    OutputKind,
    ProducerResult,
    detect_outputs,
)
from apm_cli.marketplace.builder import BuildReport, MarketplaceOutputReport


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# detect_outputs
# ---------------------------------------------------------------------------


class TestDetectOutputs:
    def test_dependencies_only_returns_bundle(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm:\n    - owner/repo\n",
        )
        assert detect_outputs(apm) == {OutputKind.BUNDLE}

    def test_marketplace_only_returns_marketplace(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\nmarketplace:\n  owner:\n    name: o\n",
        )
        assert detect_outputs(apm) == {OutputKind.MARKETPLACE}

    def test_both_blocks_present(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\n"
            "dependencies:\n  apm:\n    - owner/repo\n"
            "marketplace:\n  owner:\n    name: o\n",
        )
        assert detect_outputs(apm) == {OutputKind.BUNDLE, OutputKind.MARKETPLACE}

    def test_neither_block_returns_empty(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(apm, "name: x\nversion: 0.1.0\ndescription: y\n")
        assert detect_outputs(apm) == set()

    def test_legacy_marketplace_yml_triggers_marketplace(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(apm, "name: x\nversion: 0.1.0\ndescription: y\n")
        _write(tmp_path / "marketplace.yml", "name: m\nversion: 0.1.0\ndescription: y\n")
        assert detect_outputs(apm) == {OutputKind.MARKETPLACE}

    def test_missing_apm_yml_with_legacy_marketplace_yml(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(tmp_path / "marketplace.yml", "name: m\n")
        # apm.yml does not exist
        assert detect_outputs(apm) == {OutputKind.MARKETPLACE}

    def test_invalid_yaml_raises_build_error(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(apm, "name: : :\n")
        with pytest.raises(BuildError, match="Failed to parse"):
            detect_outputs(apm)

    def test_non_mapping_top_level_raises(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(apm, "- a\n- b\n")
        with pytest.raises(BuildError, match="must be a YAML mapping"):
            detect_outputs(apm)


# ---------------------------------------------------------------------------
# BuildOrchestrator
# ---------------------------------------------------------------------------


def _make_producer(kind: OutputKind, output_path: Path) -> ArtifactProducer:
    producer = MagicMock(spec=["kind", "produce"])
    producer.kind = kind
    producer.produce.return_value = ProducerResult(kind=kind, outputs=[output_path])
    return producer


class TestBuildOrchestrator:
    def test_runs_only_bundle_when_only_dependencies(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm:\n    - owner/repo\n",
        )
        bp = _make_producer(OutputKind.BUNDLE, tmp_path / "build")
        mp = _make_producer(OutputKind.MARKETPLACE, tmp_path / "m.json")
        opts = BuildOptions(project_root=tmp_path, apm_yml_path=apm)

        result = BuildOrchestrator(producers=[bp, mp]).run(opts)

        bp.produce.assert_called_once()
        mp.produce.assert_not_called()
        assert result.outputs == [tmp_path / "build"]

    def test_runs_only_marketplace_when_only_marketplace(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\nmarketplace:\n  owner:\n    name: o\n",
        )
        bp = _make_producer(OutputKind.BUNDLE, tmp_path / "build")
        mp = _make_producer(OutputKind.MARKETPLACE, tmp_path / "m.json")
        opts = BuildOptions(project_root=tmp_path, apm_yml_path=apm)

        result = BuildOrchestrator(producers=[bp, mp]).run(opts)

        bp.produce.assert_not_called()
        mp.produce.assert_called_once()
        assert result.outputs == [tmp_path / "m.json"]

    def test_runs_both_when_both_present(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\n"
            "dependencies:\n  apm:\n    - owner/repo\n"
            "marketplace:\n  owner:\n    name: o\n",
        )
        bp = _make_producer(OutputKind.BUNDLE, tmp_path / "build")
        mp = _make_producer(OutputKind.MARKETPLACE, tmp_path / "m.json")
        opts = BuildOptions(project_root=tmp_path, apm_yml_path=apm)

        result = BuildOrchestrator(producers=[bp, mp]).run(opts)

        bp.produce.assert_called_once()
        mp.produce.assert_called_once()
        assert set(result.outputs) == {tmp_path / "build", tmp_path / "m.json"}

    def test_raises_build_error_when_neither_block_present(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(apm, "name: x\nversion: 0.1.0\ndescription: y\n")
        opts = BuildOptions(project_root=tmp_path, apm_yml_path=apm)

        with pytest.raises(BuildError, match="Nothing to pack"):
            BuildOrchestrator().run(opts)

    def test_collects_warnings_from_all_producers(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\nversion: 0.1.0\ndescription: y\n"
            "dependencies:\n  apm:\n    - owner/repo\n"
            "marketplace:\n  owner:\n    name: o\n",
        )
        bp = MagicMock(spec=["kind", "produce"])
        bp.kind = OutputKind.BUNDLE
        bp.produce.return_value = ProducerResult(
            kind=OutputKind.BUNDLE, outputs=[], warnings=["b-warn"]
        )
        mp = MagicMock(spec=["kind", "produce"])
        mp.kind = OutputKind.MARKETPLACE
        mp.produce.return_value = ProducerResult(
            kind=OutputKind.MARKETPLACE, outputs=[], warnings=["m-warn"]
        )
        opts = BuildOptions(project_root=tmp_path, apm_yml_path=apm)

        result = BuildOrchestrator(producers=[bp, mp]).run(opts)

        assert result.warnings == ["b-warn", "m-warn"]

    def test_default_producers_are_bundle_and_marketplace(self):
        orch = BuildOrchestrator()
        kinds = [p.kind for p in orch._producers]
        assert OutputKind.BUNDLE in kinds
        assert OutputKind.MARKETPLACE in kinds


class TestMarketplaceProducer:
    def test_writes_claude_and_codex_outputs_when_requested(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\n"
            "version: 0.1.0\n"
            "description: y\n"
            "marketplace:\n"
            "  owner:\n"
            "    name: o\n"
            "  outputs: [claude, codex]\n"
            "  packages:\n"
            "    - name: local-tool\n"
            "      source: ./plugins/local-tool\n"
            "      category: Productivity\n",
        )
        opts = BuildOptions(
            project_root=tmp_path,
            apm_yml_path=apm,
            marketplace_offline=True,
        )

        result = MarketplaceProducer().produce(opts, logger=None)

        claude_output = tmp_path / ".claude-plugin" / "marketplace.json"
        codex_output = tmp_path / ".agents" / "plugins" / "marketplace.json"
        assert claude_output in result.outputs
        assert codex_output in result.outputs
        assert claude_output.exists()
        assert codex_output.exists()

    def test_writes_only_codex_when_requested(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\n"
            "version: 0.1.0\n"
            "description: y\n"
            "marketplace:\n"
            "  owner:\n"
            "    name: o\n"
            "  outputs: [codex]\n"
            "  packages:\n"
            "    - name: local-tool\n"
            "      source: ./plugins/local-tool\n"
            "      category: Productivity\n",
        )
        opts = BuildOptions(
            project_root=tmp_path,
            apm_yml_path=apm,
            marketplace_offline=True,
        )

        result = MarketplaceProducer().produce(opts, logger=None)

        claude_output = tmp_path / ".claude-plugin" / "marketplace.json"
        codex_output = tmp_path / ".agents" / "plugins" / "marketplace.json"
        assert result.payload is not None
        assert [output.profile for output in result.payload.outputs] == ["codex"]
        assert result.outputs == [codex_output]
        assert not claude_output.exists()
        assert codex_output.exists()

    def test_marketplace_output_override_applies_only_to_claude_profile(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\n"
            "version: 0.1.0\n"
            "description: y\n"
            "marketplace:\n"
            "  owner:\n"
            "    name: o\n"
            "  outputs: [claude, codex]\n"
            "  claude:\n"
            "    output: build/claude-config.json\n"
            "  codex:\n"
            "    output: build/codex-config.json\n"
            "  packages:\n"
            "    - name: local-tool\n"
            "      source: ./plugins/local-tool\n"
            "      category: Productivity\n",
        )
        override = tmp_path / "override" / "claude.json"
        opts = BuildOptions(
            project_root=tmp_path,
            apm_yml_path=apm,
            marketplace_offline=True,
            marketplace_output=override,
        )

        result = MarketplaceProducer().produce(opts, logger=None)

        codex_output = tmp_path / "build" / "codex-config.json"
        assert result.payload is not None
        assert [output.profile for output in result.payload.outputs] == ["claude", "codex"]
        assert override in result.outputs
        assert codex_output in result.outputs
        assert override.exists()
        assert codex_output.exists()
        assert not (tmp_path / "build" / "claude-config.json").exists()

    def test_manifest_config_controls_each_marketplace_output_path(self, tmp_path: Path):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\n"
            "version: 0.1.0\n"
            "description: y\n"
            "marketplace:\n"
            "  owner:\n"
            "    name: o\n"
            "  outputs: [claude, codex]\n"
            "  claude:\n"
            "    output: dist/claude-marketplace.json\n"
            "  codex:\n"
            "    output: dist/codex-marketplace.json\n"
            "  packages:\n"
            "    - name: local-tool\n"
            "      source: ./plugins/local-tool\n"
            "      category: Productivity\n",
        )
        opts = BuildOptions(
            project_root=tmp_path,
            apm_yml_path=apm,
            marketplace_offline=True,
        )

        result = MarketplaceProducer().produce(opts, logger=None)

        claude_output = tmp_path / "dist" / "claude-marketplace.json"
        codex_output = tmp_path / "dist" / "codex-marketplace.json"
        assert result.outputs == [claude_output, codex_output]
        assert claude_output.exists()
        assert codex_output.exists()

    def test_unknown_marketplace_output_target_raises_build_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\n"
            "version: 0.1.0\n"
            "description: y\n"
            "marketplace:\n"
            "  owner:\n"
            "    name: o\n"
            "  packages: []\n",
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.migration.load_marketplace_config",
            lambda *args, **kwargs: SimpleNamespace(
                outputs=("cursor",),
                source_path=apm,
            ),
        )

        with pytest.raises(BuildError, match="Unknown marketplace output target: 'cursor'"):
            MarketplaceProducer().produce(
                BuildOptions(
                    project_root=tmp_path,
                    apm_yml_path=apm,
                    marketplace_offline=True,
                ),
                logger=None,
            )

    def test_build_warnings_are_exposed_on_producer_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        apm = tmp_path / "apm.yml"
        _write(
            apm,
            "name: x\n"
            "version: 0.1.0\n"
            "description: y\n"
            "marketplace:\n"
            "  owner:\n"
            "    name: o\n"
            "  packages: []\n",
        )
        output_path = tmp_path / ".claude-plugin" / "marketplace.json"

        def fake_write_output(self, *args, **kwargs):
            return BuildReport(
                outputs=(
                    MarketplaceOutputReport(
                        profile="claude",
                        resolved=(),
                        errors=(),
                        warnings=("duplicate package warning",),
                        output_path=output_path,
                    ),
                )
            )

        monkeypatch.setattr(
            "apm_cli.marketplace.builder.MarketplaceBuilder.write_output",
            fake_write_output,
        )

        result = MarketplaceProducer().produce(
            BuildOptions(
                project_root=tmp_path,
                apm_yml_path=apm,
                marketplace_offline=True,
            ),
            logger=None,
        )

        assert result.payload is not None
        assert result.payload.warnings == ("duplicate package warning",)
        assert result.warnings == ["duplicate package warning"]
