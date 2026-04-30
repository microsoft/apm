"""Unified artifact production -- bundle + marketplace.json from one entrypoint.

The :class:`BuildOrchestrator` inspects ``apm.yml`` and runs whichever
producers are applicable:

* ``dependencies:`` block  -> :class:`BundleProducer`  -> ``./build/<name>/``
* ``marketplace:`` block   -> :class:`MarketplaceProducer` -> ``.claude-plugin/marketplace.json``

Producers are thin adapters around the existing
:func:`apm_cli.bundle.packer.pack_bundle` and
:class:`apm_cli.marketplace.builder.MarketplaceBuilder` -- the orchestrator
adds no new build logic, only routing.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml


class OutputKind(enum.Enum):
    """Kinds of artifacts that ``apm pack`` can produce."""

    BUNDLE = "bundle"
    MARKETPLACE = "marketplace"


@dataclass
class BuildOptions:
    """Knobs collected from ``apm pack`` flags and passed to producers."""

    project_root: Path
    apm_yml_path: Path
    # Bundle-only options
    bundle_format: str = "apm"
    bundle_target: Any = None
    bundle_archive: bool = False
    bundle_output: Path | None = None
    bundle_force: bool = False
    # Marketplace-only options
    marketplace_offline: bool = False
    marketplace_include_prerelease: bool = False
    marketplace_output: Path | None = None
    # Common options
    dry_run: bool = False
    verbose: bool = False


@dataclass
class ProducerResult:
    """One producer's contribution to the overall build."""

    kind: OutputKind
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    payload: Any = None


@dataclass
class BuildResult:
    """Aggregated outputs and warnings from every producer that ran."""

    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    producer_results: list[ProducerResult] = field(default_factory=list)


class BuildError(Exception):
    """User-facing build error. The CLI maps this to exit code 1."""


class ArtifactProducer(Protocol):
    """Protocol that every concrete producer must implement."""

    kind: OutputKind

    def produce(self, options: BuildOptions, logger: Any) -> ProducerResult: ...


# ---------------------------------------------------------------------------
# Bundle producer -- thin adapter around bundle.packer.pack_bundle
# ---------------------------------------------------------------------------


class BundleProducer:
    """Produce an APM bundle (or plugin bundle) from the lockfile."""

    kind = OutputKind.BUNDLE

    def produce(self, options: BuildOptions, logger: Any) -> ProducerResult:
        from ..bundle.packer import pack_bundle

        output_dir = options.bundle_output or (options.project_root / "build")
        try:
            pack_result = pack_bundle(
                project_root=options.project_root,
                output_dir=output_dir,
                fmt=options.bundle_format,
                target=options.bundle_target,
                archive=options.bundle_archive,
                dry_run=options.dry_run,
                force=options.bundle_force,
                logger=logger,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise BuildError(str(exc)) from exc

        outputs: list[Path] = []
        if pack_result.bundle_path is not None:
            outputs.append(Path(pack_result.bundle_path))
        return ProducerResult(
            kind=OutputKind.BUNDLE,
            outputs=outputs,
            payload=pack_result,
        )


# ---------------------------------------------------------------------------
# Marketplace producer -- thin adapter around MarketplaceBuilder
# ---------------------------------------------------------------------------


class MarketplaceProducer:
    """Produce ``.claude-plugin/marketplace.json`` from the marketplace block."""

    kind = OutputKind.MARKETPLACE

    def produce(self, options: BuildOptions, logger: Any) -> ProducerResult:
        from ..marketplace.builder import (
            BuildOptions as MktBuildOptions,
        )
        from ..marketplace.builder import (
            MarketplaceBuilder,
        )
        from ..marketplace.errors import BuildError as MktBuildError
        from ..marketplace.migration import (
            ConfigSource,
            detect_config_source,
            load_marketplace_config,
        )
        from ..marketplace.yml_schema import MarketplaceYmlError

        warnings: list[str] = []

        def _warn(msg: str) -> None:
            warnings.append(msg)

        project_root = options.project_root
        try:
            source = detect_config_source(project_root)
            config = load_marketplace_config(project_root, warn_callback=_warn)
        except MarketplaceYmlError as exc:
            raise BuildError(f"marketplace config error: {exc}") from exc

        # Resolve which on-disk yml the builder should bind to (purely
        # cosmetic -- the from_config path uses the loaded config object).
        if source == ConfigSource.LEGACY_YML:
            yml_for_builder = project_root / "marketplace.yml"
        else:
            yml_for_builder = project_root / "apm.yml"

        # Determine the output override: explicit flag wins; otherwise
        # legacy marketplace.yml keeps writing to ./marketplace.json (the
        # value baked into the legacy config), and apm.yml keeps writing
        # to .claude-plugin/marketplace.json (also the config default).
        output_override: Path | None = None
        if options.marketplace_output is not None:
            output_override = options.marketplace_output

        mkt_opts = MktBuildOptions(
            dry_run=options.dry_run,
            offline=options.marketplace_offline,
            include_prerelease=options.marketplace_include_prerelease,
            output_override=output_override,
        )
        builder = MarketplaceBuilder.from_config(
            config, project_root=project_root, options=mkt_opts
        )
        # Bind the synthetic yml path to the actual on-disk file when it
        # exists so any downstream diagnostics report a real location.
        builder._yml_path = yml_for_builder

        try:
            report = builder.build()
        except MktBuildError as exc:
            raise BuildError(str(exc)) from exc

        outputs: list[Path] = []
        if report.output_path is not None:
            outputs.append(Path(report.output_path))
        warnings.extend(report.warnings)
        return ProducerResult(
            kind=OutputKind.MARKETPLACE,
            outputs=outputs,
            warnings=warnings,
            payload=report,
        )


# ---------------------------------------------------------------------------
# Output detection
# ---------------------------------------------------------------------------


def detect_outputs(apm_yml_path: Path) -> set[OutputKind]:
    """Inspect ``apm.yml`` (and a sibling legacy ``marketplace.yml``) and
    return the set of producers that should run.
    """

    out: set[OutputKind] = set()
    data: dict | None = None
    if apm_yml_path.is_file():
        try:
            with open(apm_yml_path, encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise BuildError(f"Failed to parse {apm_yml_path}: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise BuildError(f"{apm_yml_path} must be a YAML mapping at the top level.")
        data = loaded or {}

    if data and data.get("dependencies"):
        out.add(OutputKind.BUNDLE)
    if data and data.get("marketplace"):
        out.add(OutputKind.MARKETPLACE)

    legacy = apm_yml_path.parent / "marketplace.yml"
    if legacy.is_file():
        out.add(OutputKind.MARKETPLACE)

    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class BuildOrchestrator:
    """Pick the right producers for an apm.yml and run them in order."""

    def __init__(
        self,
        producers: Sequence[ArtifactProducer] | None = None,
    ) -> None:
        self._producers: list[ArtifactProducer] = (
            list(producers) if producers is not None else [BundleProducer(), MarketplaceProducer()]
        )

    def run(self, options: BuildOptions, logger: Any = None) -> BuildResult:
        outputs_needed = detect_outputs(options.apm_yml_path)
        if not outputs_needed:
            raise BuildError(
                "apm.yml has neither 'dependencies:' nor 'marketplace:' "
                "block. Nothing to pack. Add dependencies via "
                "'apm install <pkg>' or scaffold a marketplace block "
                "with 'apm marketplace init'."
            )

        result = BuildResult()
        for producer in self._producers:
            if producer.kind not in outputs_needed:
                continue
            sub = producer.produce(options, logger)
            result.outputs.extend(sub.outputs)
            result.warnings.extend(sub.warnings)
            result.producer_results.append(sub)
        return result
