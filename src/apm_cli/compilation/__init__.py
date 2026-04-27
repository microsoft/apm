"""APM compilation module for generating AGENTS.md files."""

from .agents_compiler import AgentsCompiler, CompilationConfig, CompilationResult, compile_agents_md
from .link_resolver import resolve_markdown_links, validate_link_targets
from .template_builder import TemplateData, build_conditional_sections, find_chatmode_by_name

__all__ = [  # noqa: RUF022
    # Main compilation interface
    "AgentsCompiler",
    "compile_agents_md",
    "CompilationConfig",
    "CompilationResult",
    # Template building
    "build_conditional_sections",
    "TemplateData",
    "find_chatmode_by_name",
    # Link resolution
    "resolve_markdown_links",
    "validate_link_targets",
]
