"""Distributed-compiler generation helpers extracted from ``DistributedAgentsCompiler``.

Extracted to keep ``compilation.distributed_compiler`` under 400 LOC.
All functions take ``compiler`` (a ``DistributedAgentsCompiler`` instance)
as their first argument and are called only from the corresponding
delegate one-liners on the class.
"""

from __future__ import annotations

import builtins
from pathlib import Path

from ..primitives.models import Instruction, PrimitiveCollection
from ._dc_models import DirectoryMap, PlacementResult


def generate_distributed_agents_files(
    compiler,
    placement_map: builtins.dict[Path, builtins.list[Instruction]],
    primitives: PrimitiveCollection,
    source_attribution: bool = True,
) -> builtins.list[PlacementResult]:
    """Generate distributed AGENTS.md file contents.

    Args:
        compiler: ``DistributedAgentsCompiler`` instance.
        placement_map (Dict[Path, List[Instruction]]): Directory to instructions mapping.
        primitives (PrimitiveCollection): Full primitive collection.
        source_attribution (bool): Whether to include source attribution.

    Returns:
        List[PlacementResult]: List of placement results with content.
    """
    placements = []

    if not placement_map:
        from .constitution import find_constitution

        constitution_path = find_constitution(Path(compiler.base_dir))
        if constitution_path.exists():
            root_path = Path(compiler.base_dir)
            agents_path = root_path / "AGENTS.md"

            placement = PlacementResult(
                agents_path=agents_path,
                instructions=[],
                coverage_patterns=set(),
                source_attribution={"constitution": "constitution.md"}
                if source_attribution
                else {},
            )

            placements.append(placement)
    else:
        for dir_path, instructions in placement_map.items():
            agents_path = dir_path / "AGENTS.md"

            source_map = {}
            if source_attribution:
                for instruction in instructions:
                    source_info = getattr(instruction, "source", "local")
                    source_map[str(instruction.file_path)] = source_info

            patterns = set()
            for instruction in instructions:
                if instruction.apply_to:
                    patterns.add(instruction.apply_to)

            placement = PlacementResult(
                agents_path=agents_path,
                instructions=instructions,
                coverage_patterns=patterns,
                source_attribution=source_map,
            )

            placements.append(placement)

    return placements


def _extract_directories_from_pattern(compiler, pattern: str) -> builtins.list[Path]:
    """Extract potential directory paths from a file pattern.

    Args:
        compiler: ``DistributedAgentsCompiler`` instance (unused; included for API symmetry).
        pattern (str): File pattern like "src/**/*.py" or "docs/*.md"

    Returns:
        List[Path]: List of directory paths that could contain matching files.
    """
    directories = []

    if pattern.startswith("**/"):
        directories.append(Path("."))
    elif "/" in pattern:
        dir_part = pattern.split("/")[0]
        if not dir_part.startswith("*"):
            directories.append(Path(dir_part))
        else:
            directories.append(Path("."))
    else:
        directories.append(Path("."))

    return directories


def _find_best_directory(
    compiler, instruction: Instruction, directory_map: DirectoryMap, max_depth: int
) -> Path:
    """Find the best directory for placing an instruction.

    Args:
        compiler: ``DistributedAgentsCompiler`` instance providing ``base_dir``.
        instruction (Instruction): Instruction to place.
        directory_map (DirectoryMap): Directory structure analysis.
        max_depth (int): Maximum allowed depth.

    Returns:
        Path: Best directory path for the instruction.
    """
    if not instruction.apply_to:
        return compiler.base_dir

    pattern = instruction.apply_to
    best_dir = compiler.base_dir
    best_specificity = 0

    for dir_path in directory_map.directories:
        if directory_map.depth_map.get(dir_path, 0) > max_depth:
            continue

        if pattern in directory_map.directories[dir_path]:
            specificity = directory_map.depth_map.get(dir_path, 0)
            if specificity > best_specificity:
                best_specificity = specificity
                best_dir = dir_path

    return best_dir
