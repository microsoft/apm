"""APM compilation module for generating AGENTS.md files."""

__all__ = [  # noqa: RUF022
    # Main compilation interface
    "AgentsCompiler",
    "compile_agents_md",
    "CompilationConfig",
    "CompilationResult",
    # User-scope root context compilation
    "compile_user_root_contexts",
    "UserRootCompileResult",
    # Template building
    "build_conditional_sections",
    "render_instructions_block",
    "TemplateData",
    "find_chatmode_by_name",
    # Link resolution
    "resolve_markdown_links",
    "validate_link_targets",
]

_LAZY = {
    "AgentsCompiler": ("agents_compiler", "AgentsCompiler"),
    "CompilationConfig": ("agents_compiler", "CompilationConfig"),
    "CompilationResult": ("agents_compiler", "CompilationResult"),
    "compile_agents_md": ("agents_compiler", "compile_agents_md"),
    "resolve_markdown_links": ("link_resolver", "resolve_markdown_links"),
    "validate_link_targets": ("link_resolver", "validate_link_targets"),
    "TemplateData": ("template_builder", "TemplateData"),
    "build_conditional_sections": ("template_builder", "build_conditional_sections"),
    "find_chatmode_by_name": ("template_builder", "find_chatmode_by_name"),
    "render_instructions_block": ("template_builder", "render_instructions_block"),
    "UserRootCompileResult": ("user_root_context", "UserRootCompileResult"),
    "compile_user_root_contexts": ("user_root_context", "compile_user_root_contexts"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f".{target[0]}", __name__), target[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
