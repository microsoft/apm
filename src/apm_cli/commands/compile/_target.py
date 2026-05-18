"""Target resolution helpers for the compile command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...constants import APM_YML_FILENAME


def _family_of(name: str) -> str | None:
    """Return the compile family for a target name, treating ``vscode`` specially."""
    if name == "vscode":
        return "vscode"
    from ...integration.targets import KNOWN_TARGETS

    profile = KNOWN_TARGETS.get(name)
    return profile.compile_family if profile else None


def _build_families(target_set: set) -> set[str]:
    """Build the set of compile families for a set of target names."""
    families: set[str] = set()
    for name in target_set:
        family = _family_of(name)
        if family is None:
            continue
        families.add(family)
        if family == "vscode":
            # copilot also emits AGENTS.md; mirror legacy behavior.
            families.add("agents")
    return families


def _agents_family_fallback(target_set: set) -> str:
    """Return the first agents-family target from the registry, or ``'vscode'``."""
    from ...integration.targets import KNOWN_TARGETS

    for name, profile in KNOWN_TARGETS.items():
        if profile.compile_family == "agents" and name in target_set:
            return name
    return "vscode"  # defensive fallback (unreachable)


def _pick_family_result(families: set, target_set: set) -> str | frozenset:
    """Pick the compile target string/frozenset from the resolved family set."""
    if len(families) >= 2:
        # Single-target copilot collapses {"vscode","agents"} to bare
        # "vscode" for routing parity with single-string -t copilot.
        return "vscode" if families == {"vscode", "agents"} else frozenset(families)
    for fam in ("claude", "gemini", "vscode"):
        if fam in families:
            return fam
    # Bare agents-family target: preserve original target name for
    # single-element list routing (e.g. -t cursor == -t [cursor]).
    return _agents_family_fallback(target_set)


def _resolve_compile_target(target):
    """Map CLI target input to a compiler-understood target.

    The compiler understands single-string targets (``"vscode"``,
    ``"claude"``, ``"gemini"``, ``"all"``) and ``frozenset`` targets
    containing compiler-family names (``"agents"``, ``"claude"``,
    ``"gemini"``).

    Multi-target lists are mapped to the narrowest representation:
    a single string when only one compiler family is needed, or a
    ``frozenset`` of families when multiple are needed.  This avoids
    collapsing to ``"all"`` (which would incorrectly generate files
    for every family).

    Family resolution reads ``TargetProfile.compile_family`` from
    ``KNOWN_TARGETS`` so adding a new compile-eligible target only
    requires populating that field.  The CLI alias ``"vscode"`` is
    treated as ``"copilot"`` for this purpose.

    Args:
        target: A single target string, a list of target strings, or ``None``.

    Returns:
        A single string, a ``frozenset`` of compiler families, or ``None``.
    """
    if target is None:
        return None  # will trigger detect_target() auto-detection
    if isinstance(target, list):
        from ...integration.targets import KNOWN_TARGETS

        target_set = set(target)
        # Strip targets with no compile output (compile_family is None);
        # they would silently fall through the family resolution otherwise.
        skip = {name for name, profile in KNOWN_TARGETS.items() if profile.compile_family is None}
        target_set -= skip
        if not target_set:
            # Solo agent-skills (or another no-compile target) in a list --
            # pass through as a string so the compiler's no-op path fires.
            for sentinel in target:
                if sentinel in skip:
                    return sentinel
            return None
        return _pick_family_result(_build_families(target_set), target_set)
    return target  # single string pass-through


def _load_config_target(apm_yml_path: Path):
    """Load target or targets from apm.yml."""
    from ...models.apm_package import APMPackage

    if not apm_yml_path.exists():
        return None
    apm_pkg = APMPackage.from_apm_yml(apm_yml_path)
    if apm_pkg.target is not None:
        return apm_pkg.target
    try:
        from ...core.apm_yml import parse_targets_field
        from ...utils.yaml_io import load_yaml

        raw = load_yaml(apm_yml_path)
        if not isinstance(raw, dict):
            return None
        yaml_targets = parse_targets_field(raw)
        if not yaml_targets:
            return None
        return yaml_targets[0] if len(yaml_targets) == 1 else yaml_targets
    except Exception:
        return None


def _resolve_effective_target(target):
    """Resolve CLI/config target input to the compiler target and reason."""
    from ...core.target_detection import detect_target

    config_target = _load_config_target(Path(APM_YML_FILENAME))
    compile_target = _resolve_compile_target(target)
    compile_config_target = _resolve_compile_target(config_target)
    if isinstance(compile_target, frozenset):
        return compile_target, "explicit --target flag", config_target
    if isinstance(compile_config_target, frozenset) and compile_target is None:
        return compile_config_target, "apm.yml target", config_target
    detected_target, detection_reason = detect_target(
        project_root=Path("."),
        explicit_target=compile_target,
        config_target=compile_config_target if isinstance(compile_config_target, str) else None,
    )
    return detected_target, detection_reason, config_target


def _coerce_provenance_targets(value):
    """Coerce target provenance input to a list of target labels."""
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(t) for t in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return []


def _emit_target_provenance(target, config_target, effective_target, detection_reason) -> None:
    """Emit the canonical target provenance line."""
    from ...core.target_detection import ResolvedTargets, format_provenance
    from ...utils.console import _rich_info

    if detection_reason == "explicit --target flag":
        provenance_targets = _coerce_provenance_targets(target)
        provenance_source = "--target flag"
    elif detection_reason == "apm.yml target":
        provenance_targets = _coerce_provenance_targets(config_target)
        provenance_source = "apm.yml"
    else:
        provenance_targets = _coerce_provenance_targets(effective_target)
        provenance_source = f"auto-detect ({detection_reason})"
    if provenance_targets:
        _rich_info(
            format_provenance(
                ResolvedTargets(
                    targets=sorted(set(provenance_targets)),
                    source=provenance_source,
                    auto_create=True,
                )
            ),
            symbol="info",
        )


@dataclass(frozen=True, slots=True)
class _CompileStrategyContext:
    """Inputs required to describe the chosen compile target strategy."""

    target: object
    config_target: object
    effective_target: object
    detection_reason: str


def _log_compile_strategy(logger, config, context: _CompileStrategyContext) -> None:
    """Render the target-aware compilation mode line."""
    from ...core.target_detection import (
        REASON_NO_TARGET_FOLDER,
        get_target_description,
        should_compile_agents_md,
        should_compile_claude_md,
        should_compile_gemini_md,
    )

    if config.strategy != "distributed" or config.single_agents:
        logger.progress("Using single-file compilation (legacy mode)", symbol="page")
        return
    effective_target = context.effective_target
    if isinstance(effective_target, frozenset):
        if isinstance(context.target, list):
            target_label = f"--target {','.join(context.target)}"
        elif isinstance(context.config_target, list):
            target_label = f"apm.yml target: [{', '.join(context.config_target)}]"
        else:
            target_label = "multi-target"
        parts = []
        if should_compile_agents_md(effective_target):
            parts.append("AGENTS.md")
        if should_compile_claude_md(effective_target):
            parts.append("CLAUDE.md")
        if should_compile_gemini_md(effective_target):
            parts.append("GEMINI.md")
        logger.progress(f"Compiling for {' + '.join(parts)} ({target_label})")
        return
    if (
        isinstance(effective_target, str)
        and effective_target == "vscode"
        and context.detection_reason == REASON_NO_TARGET_FOLDER
    ):
        logger.progress(f"Compiling for AGENTS.md only ({context.detection_reason})")
        logger.progress(
            " Create .github/, .claude/, .codex/, .opencode/ or .cursor/ folder for full integration",
            symbol="light_bulb",
        )
        return
    logger.progress(
        f"Compiling for {get_target_description(effective_target)} - {context.detection_reason}"
    )
