"""Shared native-rule coverage checks for project and user-root compilation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from apm_cli.primitives.models import Instruction, PrimitiveCollection
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within


def _safe_rules_dir(rules_dir: Path, base_dir: Path, warn_fn: Callable[[str], None]) -> bool:
    """Check the native rules directory without trusting an escaping symlink."""
    try:
        if not rules_dir.is_dir():
            return False
        ensure_path_within(rules_dir, base_dir)
    except (OSError, RuntimeError, PathTraversalError) as exc:
        warn_fn(f"Cannot verify native rules directory {rules_dir}: {exc}; ignoring native rules")
        return False
    return True


def _safe_rule_path(path: Path, base_dir: Path, warn_fn: Callable[[str], None]) -> Path | None:
    """Return a contained regular rule file, or retain the compiled fallback."""
    try:
        ensure_path_within(path, base_dir)
        return path if path.is_file() else None
    except (OSError, RuntimeError, PathTraversalError) as exc:
        warn_fn(f"Cannot verify native rule {path}: {exc}; retaining compiled instructions")
        return None


def detect_deployed_instructions(
    rules_dir: Path,
    base_dir: Path,
    warn_fn: Callable[[str], None],
    expected_filenames: set[str] | None = None,
) -> bool:
    """Preserve project compilation's any-matching-rule deduplication policy."""
    if not _safe_rules_dir(rules_dir, base_dir, warn_fn):
        return False
    candidates = (
        (rules_dir / name for name in expected_filenames)
        if expected_filenames is not None
        else rules_dir.glob("*.md")
    )
    return any(_safe_rule_path(path, base_dir, warn_fn) is not None for path in candidates)


def build_expected_rule_filenames(target_key: str, primitives: PrimitiveCollection) -> set[str]:
    """Derive deployed instruction names from the integrator's filename owner."""
    from apm_cli.integration.instruction_integrator import instruction_rule_filename
    from apm_cli.integration.targets import KNOWN_TARGETS

    target = KNOWN_TARGETS.get(target_key)
    mapping = target.primitives.get("instructions") if target else None
    if mapping is None:
        return set()
    return {
        instruction_rule_filename(instruction.file_path, mapping.extension)
        for instruction in primitives.instructions
    }


def uncovered_instructions(
    target_key: str,
    instructions: list[Instruction],
    rules_dir: Path,
    base_dir: Path,
    warn_fn: Callable[[str], None],
) -> list[Instruction]:
    """Retain instructions not represented by equivalent installed native rules.

    Unlike the project compiler's historical any-match policy, user-root
    compilation checks every instruction. The install renderer is reused so
    converted frontmatter and rewritten package links participate in equality.
    Missing, unreadable, unsafe, or different rules keep the compiled fallback.
    """
    from apm_cli.integration.instruction_integrator import (
        InstructionIntegrator,
        instruction_rule_filename,
    )
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    target = KNOWN_TARGETS.get(target_key)
    mapping = target.primitives.get("instructions") if target else None
    if mapping is None or not _safe_rules_dir(rules_dir, base_dir, warn_fn):
        return list(instructions)

    integrators: dict[Path, InstructionIntegrator] = {}
    uncovered: list[Instruction] = []
    for instruction in instructions:
        filename = instruction_rule_filename(instruction.file_path, mapping.extension)
        rule_path = _safe_rule_path(rules_dir / filename, base_dir, warn_fn)
        if rule_path is None or instruction.apply_to:
            uncovered.append(instruction)
            continue

        # Native instructions are installed from a package's .apm/instructions.
        # Discovery also finds generic instruction files; without a package
        # boundary their installed link projection cannot be established safely.
        source = instruction.file_path
        apm_dir = next((parent for parent in source.parents if parent.name == ".apm"), None)
        if apm_dir is None or not source.is_relative_to(apm_dir / "instructions"):
            uncovered.append(instruction)
            continue
        package_root = apm_dir.parent
        try:
            ensure_path_within(source, package_root)
            integrator = integrators.get(package_root)
            if integrator is None:
                integrator = InstructionIntegrator()
                integrator.init_link_resolver(
                    PackageInfo(
                        # Link resolution uses only the package source path;
                        # discovery need not have a package manifest available.
                        package=APMPackage(name=package_root.name, version="0.0.0"),
                        install_path=package_root,
                    ),
                    base_dir,
                )
                integrators[package_root] = integrator
            if integrator.link_resolver is None:
                uncovered.append(instruction)
                continue
            expected, _ = integrator._render_instruction(source, rule_path, mapping.format_id)
            if rule_path.read_text(encoding="utf-8").strip() != expected.strip():
                uncovered.append(instruction)
        except (OSError, RuntimeError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            warn_fn(
                f"Cannot verify native rule {rule_path}: {exc}; retaining compiled instructions"
            )
            uncovered.append(instruction)
    return uncovered
