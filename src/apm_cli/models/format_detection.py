"""Package-format detection: detectors, registry, report, and planner.

This module implements the composition model for package-format detection
described in issue #782. Each format has its own ``FormatDetector`` that
inspects the filesystem and returns per-format evidence independently of
the others. ``PackageFormatRegistry`` runs all detectors and produces a
``DetectionReport`` (a set of per-format evidences -- not a single type).
``NormalizationPlanner`` reads the report and resolves which
``PackageType`` to assign (backward-compat) and, in future, which
normalizer callables to invoke.

This replaces the positional-priority if/elif cascade in
``detect_package_type`` so that new formats are additive and ordering
bugs are structurally impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .validation import PackageType

from ..constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME

# Canonical directory names that form part of the Claude Code marketplace
# plugin layout.  Order is preserved in ``ClaudePluginFormatEvidence``
# and asserted by existing tests.
_PLUGIN_DIRS: tuple[str, ...] = ("agents", "skills", "commands")


# ---------------------------------------------------------------------------
# Per-format evidence dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApmYmlFormatEvidence:
    """File-system signals gathered by ``ApmYmlDetector``.

    Fields
    ------
    apm_yml_path:
        Absolute path to the ``apm.yml`` that was found.
    has_apm_dir:
        ``True`` iff a ``.apm/`` directory exists alongside ``apm.yml``.
    declares_dependencies:
        ``True`` iff ``apm.yml`` lists at least one dependency under
        ``dependencies.apm``, ``dependencies.mcp``,
        ``devDependencies.apm``, or ``devDependencies.mcp``.
    """

    apm_yml_path: Path
    has_apm_dir: bool
    declares_dependencies: bool


@dataclass(frozen=True)
class SkillMdFormatEvidence:
    """File-system signals gathered by ``SkillMdDetector``.

    Fields
    ------
    skill_md_path:
        Path to the *root* ``SKILL.md`` if present, ``None`` otherwise.
    nested_skill_dirs:
        Names of ``skills/<name>/`` directories that contain a
        ``SKILL.md`` (canonical sorted order).
    """

    skill_md_path: Path | None
    nested_skill_dirs: tuple[str, ...]


@dataclass(frozen=True)
class HookJsonFormatEvidence:
    """File-system signals gathered by ``HookJsonDetector``.

    Fields
    ------
    hooks_dirs_found:
        Directories (``hooks/`` or ``.apm/hooks/``) in which at least
        one ``*.json`` file was discovered.
    """

    hooks_dirs_found: tuple[Path, ...]


@dataclass(frozen=True)
class ClaudePluginFormatEvidence:
    """File-system signals gathered by ``ClaudePluginDetector``.

    Fields
    ------
    plugin_json_path:
        Path to ``plugin.json`` (in one of the spec-defined locations)
        if found, ``None`` otherwise.
    has_claude_plugin_dir:
        ``True`` iff a ``.claude-plugin/`` directory is present at the
        package root.
    plugin_dirs_present:
        Subset of ``("agents", "skills", "commands")`` whose directories
        exist under the package root (canonical order).
    """

    plugin_json_path: Path | None
    has_claude_plugin_dir: bool
    plugin_dirs_present: tuple[str, ...]


# ---------------------------------------------------------------------------
# DetectionReport -- aggregated output of PackageFormatRegistry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectionReport:
    """Aggregated result of running all ``FormatDetector`` instances.

    Each field holds the per-format evidence produced by its detector, or
    ``None`` when that detector found no signals for its format.  All four
    detectors run independently, so the report can capture mixed packages
    (e.g. a Claude plugin that also ships hooks).

    This object doubles as the observability payload -- verbose detection
    traces and near-miss warnings can be derived from it without a second
    filesystem scan.
    """

    apm_yml: ApmYmlFormatEvidence | None = None
    skill_md: SkillMdFormatEvidence | None = None
    hook_json: HookJsonFormatEvidence | None = None
    claude_plugin: ClaudePluginFormatEvidence | None = None


# ---------------------------------------------------------------------------
# FormatDetector protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FormatDetector(Protocol):
    """Detector interface: inspect a directory for one format's signals.

    Implementations are pure (no side-effects, no file mutations) and
    return ``None`` when their format is absent.
    """

    def detect(
        self, package_path: Path
    ) -> (
        ApmYmlFormatEvidence
        | SkillMdFormatEvidence
        | HookJsonFormatEvidence
        | ClaudePluginFormatEvidence
        | None
    ):
        """Inspect ``package_path`` and return per-format evidence, or ``None``."""
        ...


# ---------------------------------------------------------------------------
# Detector implementations
# ---------------------------------------------------------------------------


def _collect_nested_skill_dirs(package_path: Path) -> tuple[str, ...]:
    """Return sorted names of ``skills/<name>/`` dirs containing a SKILL.md."""
    skills_dir = package_path / "skills"
    if not skills_dir.is_dir():
        return ()
    return tuple(
        d.name
        for d in sorted(skills_dir.iterdir())
        if d.is_dir() and (d / SKILL_MD_FILENAME).exists()
    )


def _check_has_hook_json(package_path: Path) -> tuple[Path, ...]:
    """Return dirs (hooks/ or .apm/hooks/) that contain at least one *.json."""
    found: list[Path] = []
    for hooks_dir in [package_path / "hooks", package_path / APM_DIR / "hooks"]:
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):
            found.append(hooks_dir)
    return tuple(found)


class ApmYmlDetector:
    """Detects ``apm.yml``-based package signals.

    Returns ``ApmYmlFormatEvidence`` when ``apm.yml`` is present,
    ``None`` otherwise.
    """

    def detect(self, package_path: Path) -> ApmYmlFormatEvidence | None:
        apm_yml_path = package_path / APM_YML_FILENAME
        if not apm_yml_path.exists():
            return None
        has_apm_dir = (package_path / APM_DIR).is_dir()
        if has_apm_dir:
            # .apm/ directory present -- APM_PACKAGE eligibility is already
            # determined; skip YAML parsing on this hot path.
            return ApmYmlFormatEvidence(
                apm_yml_path=apm_yml_path,
                has_apm_dir=True,
                declares_dependencies=False,
            )
        from .validation import _apm_yml_declares_dependencies

        declares_deps = _apm_yml_declares_dependencies(apm_yml_path)
        return ApmYmlFormatEvidence(
            apm_yml_path=apm_yml_path,
            has_apm_dir=False,
            declares_dependencies=declares_deps,
        )


class SkillMdDetector:
    """Detects root ``SKILL.md`` and nested ``skills/<name>/SKILL.md`` signals.

    Returns ``SkillMdFormatEvidence`` when either a root SKILL.md or
    nested skill directories are found, ``None`` otherwise.
    """

    def detect(self, package_path: Path) -> SkillMdFormatEvidence | None:
        skill_md_path = package_path / SKILL_MD_FILENAME
        root_found = skill_md_path.exists()
        nested = _collect_nested_skill_dirs(package_path)
        if not root_found and not nested:
            return None
        return SkillMdFormatEvidence(
            skill_md_path=skill_md_path if root_found else None,
            nested_skill_dirs=nested,
        )


class HookJsonDetector:
    """Detects ``hooks/*.json`` signals.

    Returns ``HookJsonFormatEvidence`` when at least one hook JSON file
    is found, ``None`` otherwise.
    """

    def detect(self, package_path: Path) -> HookJsonFormatEvidence | None:
        dirs_found = _check_has_hook_json(package_path)
        if not dirs_found:
            return None
        return HookJsonFormatEvidence(hooks_dirs_found=dirs_found)


class ClaudePluginDetector:
    """Detects Claude Code marketplace plugin signals.

    Returns ``ClaudePluginFormatEvidence`` when a plugin manifest
    (``plugin.json`` or ``.claude-plugin/``) is present, ``None``
    otherwise.
    """

    def detect(self, package_path: Path) -> ClaudePluginFormatEvidence | None:
        from ..utils.helpers import find_plugin_json

        plugin_json_path = find_plugin_json(package_path)
        has_claude_plugin_dir = (package_path / ".claude-plugin").is_dir()
        plugin_dirs_present = tuple(name for name in _PLUGIN_DIRS if (package_path / name).is_dir())
        if plugin_json_path is None and not has_claude_plugin_dir:
            return None
        return ClaudePluginFormatEvidence(
            plugin_json_path=plugin_json_path,
            has_claude_plugin_dir=has_claude_plugin_dir,
            plugin_dirs_present=plugin_dirs_present,
        )


# ---------------------------------------------------------------------------
# PackageFormatRegistry
# ---------------------------------------------------------------------------

# Default ordered set of detectors.  Order here does NOT affect
# classification priority (that lives in NormalizationPlanner); all
# detectors always run.
_DEFAULT_DETECTORS: tuple[
    ApmYmlDetector | SkillMdDetector | HookJsonDetector | ClaudePluginDetector,
    ...,
] = (
    ClaudePluginDetector(),
    SkillMdDetector(),
    ApmYmlDetector(),
    HookJsonDetector(),
)


class PackageFormatRegistry:
    """Runs all registered ``FormatDetector`` instances and collects a report.

    All detectors run independently for every call to :meth:`detect`,
    ensuring that mixed packages are captured fully.  The resulting
    ``DetectionReport`` holds ``None`` for any format that was absent.
    """

    def __init__(
        self,
        detectors: (
            tuple[
                ApmYmlDetector | SkillMdDetector | HookJsonDetector | ClaudePluginDetector,
                ...,
            ]
            | None
        ) = None,
    ) -> None:
        self._detectors = detectors if detectors is not None else _DEFAULT_DETECTORS

    def detect(self, package_path: Path) -> DetectionReport:
        """Run all detectors against ``package_path`` and return the report."""
        apm_yml_ev: ApmYmlFormatEvidence | None = None
        skill_md_ev: SkillMdFormatEvidence | None = None
        hook_json_ev: HookJsonFormatEvidence | None = None
        claude_plugin_ev: ClaudePluginFormatEvidence | None = None

        for detector in self._detectors:
            result = detector.detect(package_path)
            if isinstance(result, ApmYmlFormatEvidence):
                apm_yml_ev = result
            elif isinstance(result, SkillMdFormatEvidence):
                skill_md_ev = result
            elif isinstance(result, HookJsonFormatEvidence):
                hook_json_ev = result
            elif isinstance(result, ClaudePluginFormatEvidence):
                claude_plugin_ev = result

        return DetectionReport(
            apm_yml=apm_yml_ev,
            skill_md=skill_md_ev,
            hook_json=hook_json_ev,
            claude_plugin=claude_plugin_ev,
        )


# ---------------------------------------------------------------------------
# NormalizationPlanner
# ---------------------------------------------------------------------------


class NormalizationPlanner:
    """Maps a ``DetectionReport`` to a ``(PackageType, plugin_json_path)`` tuple.

    Encodes the same cascade priority as the old if/elif chain, but the
    logic is now explicit and the source of each branch is traceable to
    the independent detector that produced the evidence.

    Cascade (first match wins):

    1. ``MARKETPLACE_PLUGIN`` -- Claude plugin detector found a manifest
       (``plugin.json`` or ``.claude-plugin/``).
    2. ``HYBRID`` -- root ``SKILL.md`` AND ``apm.yml`` both present.
    3. ``CLAUDE_SKILL`` -- root ``SKILL.md`` only (no ``apm.yml``).
    4. ``SKILL_BUNDLE`` -- nested ``skills/<name>/SKILL.md`` found.
    5. ``APM_PACKAGE`` -- ``apm.yml`` with ``.apm/`` or declared deps.
    6. ``HOOK_PACKAGE`` -- hooks JSON found, nothing else.
    7. ``INVALID`` -- no recognisable signals.

    Future: :meth:`plan_normalizers` will return an ordered list of
    normalizer callables so mixed packages can run multiple passes.
    """

    def plan(self, report: DetectionReport) -> tuple[PackageType, Path | None]:
        """Resolve ``PackageType`` and optional ``plugin_json_path`` from report.

        Returns a ``(PackageType, plugin_json_path)`` tuple identical in
        shape and semantics to the old ``detect_package_type`` return value.
        """
        from .validation import PackageType

        cp = report.claude_plugin
        sm = report.skill_md
        ay = report.apm_yml
        hj = report.hook_json

        # 1. Claude plugin manifest present -> MARKETPLACE_PLUGIN
        if cp is not None:
            return PackageType.MARKETPLACE_PLUGIN, cp.plugin_json_path

        # 2. Root SKILL.md + apm.yml -> HYBRID
        has_root_skill_md = sm is not None and sm.skill_md_path is not None
        if ay is not None and has_root_skill_md:
            return PackageType.HYBRID, None

        # 3. Root SKILL.md only (no apm.yml) -> CLAUDE_SKILL
        if has_root_skill_md:
            return PackageType.CLAUDE_SKILL, None

        # 4. Nested skills/<name>/SKILL.md -> SKILL_BUNDLE (apm.yml optional)
        if sm is not None and sm.nested_skill_dirs:
            return PackageType.SKILL_BUNDLE, None

        # 5. apm.yml present -> APM classification
        if ay is not None:
            if ay.has_apm_dir or ay.declares_dependencies:
                return PackageType.APM_PACKAGE, None
            return PackageType.INVALID, None

        # 6. hooks/*.json only -> HOOK_PACKAGE
        if hj is not None:
            return PackageType.HOOK_PACKAGE, None

        # 7. Nothing recognisable -> INVALID
        return PackageType.INVALID, None
