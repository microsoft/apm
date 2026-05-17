"""APM compilation module for generating AGENTS.md files."""

from .agents_compiler import AgentsCompiler, CompilationConfig, CompilationResult, compile_agents_md
from .link_resolver import resolve_markdown_links, validate_link_targets
from .template_builder import (
    TemplateData,
    build_conditional_sections,
    find_chatmode_by_name,
    render_instructions_block,
)

__all__ = [
    # Main compilation interface
    "AgentsCompiler",
    "CompilationConfig",
    "CompilationResult",
    "TemplateData",
    # Template building
    "build_conditional_sections",
    "compile_agents_md",
    "find_chatmode_by_name",
    "render_instructions_block",
    # Link resolution
    "resolve_markdown_links",
    "validate_link_targets",
]
