"""Apply apm.yml ``compilation:`` settings onto a CompilationConfig."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agents_compiler import CompilationConfig


def apply_compilation_section(config: CompilationConfig, compilation_config: dict) -> None:
    """Copy the ``compilation:`` keys of apm.yml onto *config* in place."""
    if "output" in compilation_config:
        config.output_path = compilation_config["output"]
    if "chatmode" in compilation_config:
        config.chatmode = compilation_config["chatmode"]
    if "resolve_links" in compilation_config:
        config.resolve_links = compilation_config["resolve_links"]
    # architecture-authority-exempt: compilation-target-field
    if "target" in compilation_config:
        # architecture-authority-exempt: compilation-target-field
        config.target = compilation_config["target"]

    # Distributed compilation settings (Task 7)
    if "strategy" in compilation_config:
        config.strategy = compilation_config["strategy"]
    if "single_file" in compilation_config:
        # Legacy config support - if single_file is True, override strategy
        if compilation_config["single_file"]:
            config.strategy = "single-file"
            config.single_agents = True

    # Placement settings
    placement_config = compilation_config.get("placement", {})
    if "min_instructions_per_file" in placement_config:
        config.min_instructions_per_file = placement_config["min_instructions_per_file"]

    # Source attribution
    if "source_attribution" in compilation_config:
        config.source_attribution = compilation_config["source_attribution"]

    # Directory exclusion patterns
    if "exclude" in compilation_config:
        exclude_patterns = compilation_config["exclude"]
        if isinstance(exclude_patterns, list):
            config.exclude = exclude_patterns
        elif isinstance(exclude_patterns, str):
            # Support single pattern as string
            config.exclude = [exclude_patterns]

    # Managed-section mode (issue #1540)
    agents_md_config = compilation_config.get("agents_md", {})
    if "mode" in agents_md_config:
        config.agents_md_mode = agents_md_config["mode"]
    if "start_marker" in agents_md_config:
        config.agents_md_start_marker = agents_md_config["start_marker"]
    if "end_marker" in agents_md_config:
        config.agents_md_end_marker = agents_md_config["end_marker"]
