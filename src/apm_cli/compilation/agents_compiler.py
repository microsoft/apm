"""Main compilation orchestration for AGENTS.md generation.

Timestamp generation removed in favor of deterministic Build ID handled after
full content assembly. This keeps repeated compiles byte-identical when source
primitives & constitution are unchanged.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any
from ..primitives.models import PrimitiveCollection
from ..primitives.discovery import discover_primitives
from ..version import get_version
from .claude_formatter import ClaudeFormatter
from .template_builder import (
    build_conditional_sections,
    generate_agents_md_template,
    TemplateData,
    find_chatmode_by_name
)
from .link_resolver import resolve_markdown_links, validate_link_targets
from ..utils.paths import portable_relpath
from ..core.target_detection import should_compile_agents_md, should_compile_claude_md, should_compile_gemini_md

_logger = logging.getLogger(__name__)


# User-facing target aliases that map to the canonical "vscode" target.
# Kept in sync with target_detection.detect_target().
_VSCODE_TARGET_ALIASES = ("copilot", "agents")
_KNOWN_TARGETS = (
    "vscode", "claude", "cursor", "opencode", "codex", "gemini", "all", "minimal",
) + _VSCODE_TARGET_ALIASES


@dataclass
class CompilationConfig:
    """Configuration for AGENTS.md compilation."""
    output_path: str = "AGENTS.md"
    chatmode: Optional[str] = None
    resolve_links: bool = True
    dry_run: bool = False
    with_constitution: bool = True  # Phase 0 feature flag
    
    # Multi-target compilation settings
    # "vscode" or "agents" -> AGENTS.md + .github/
    # "claude" -> CLAUDE.md + .claude/
    # "all" -> both targets
    target: str = "all"
    
    # Distributed compilation settings (Task 7)
    strategy: str = "distributed"  # "distributed" or "single-file"
    single_agents: bool = False  # Force single-file mode
    trace: bool = False  # Show source attribution and conflicts
    local_only: bool = False  # Ignore dependencies, compile only local primitives
    debug: bool = False  # Show context optimizer analysis and metrics
    min_instructions_per_file: int = 1  # Minimum instructions per AGENTS.md file (Minimal Context Principle)
    source_attribution: bool = True  # Include source file comments
    clean_orphaned: bool = False  # Remove orphaned AGENTS.md files
    exclude: List[str] = None  # Glob patterns for directories to exclude during compilation
    
    def __post_init__(self):
        """Handle CLI flag precedence after initialization."""
        if self.single_agents:
            self.strategy = "single-file"
        # Initialize exclude list if None
        if self.exclude is None:
            self.exclude = []
    
    @classmethod
    def from_apm_yml(cls, **overrides) -> 'CompilationConfig':
        """Create configuration from apm.yml with command-line overrides.
        
        Args:
            **overrides: Command-line arguments that override config file values.
            
        Returns:
            CompilationConfig: Configuration with apm.yml values and overrides applied.
        """
        config = cls()
        
        # Try to load from apm.yml
        try:
            from pathlib import Path
            
            if Path('apm.yml').exists():
                from ..utils.yaml_io import load_yaml
                apm_config = load_yaml('apm.yml') or {}
                
                # Look for compilation section
                compilation_config = apm_config.get('compilation', {})
                
                # Apply config file values
                if 'output' in compilation_config:
                    config.output_path = compilation_config['output']
                if 'chatmode' in compilation_config:
                    config.chatmode = compilation_config['chatmode']
                if 'resolve_links' in compilation_config:
                    config.resolve_links = compilation_config['resolve_links']
                if 'target' in compilation_config:
                    config.target = compilation_config['target']
                
                # Distributed compilation settings (Task 7)
                if 'strategy' in compilation_config:
                    config.strategy = compilation_config['strategy']
                if 'single_file' in compilation_config:
                    # Legacy config support - if single_file is True, override strategy
                    if compilation_config['single_file']:
                        config.strategy = "single-file"
                        config.single_agents = True
                
                # Placement settings
                placement_config = compilation_config.get('placement', {})
                if 'min_instructions_per_file' in placement_config:
                    config.min_instructions_per_file = placement_config['min_instructions_per_file']
                
                # Source attribution
                if 'source_attribution' in compilation_config:
                    config.source_attribution = compilation_config['source_attribution']
                
                # Directory exclusion patterns
                if 'exclude' in compilation_config:
                    exclude_patterns = compilation_config['exclude']
                    if isinstance(exclude_patterns, list):
                        config.exclude = exclude_patterns
                    elif isinstance(exclude_patterns, str):
                        # Support single pattern as string
                        config.exclude = [exclude_patterns]
                
        except Exception as exc:
            _logger.debug("Config loading failed, using defaults: %s", exc)
        
        # Apply command-line overrides (highest priority)
        for key, value in overrides.items():
            if value is not None:  # Only override if explicitly provided
                setattr(config, key, value)
        
        # Handle CLI flag precedence
        if config.single_agents:
            config.strategy = "single-file"
        
        return config


@dataclass
class CompilationResult:
    """Result of AGENTS.md compilation."""
    success: bool
    output_path: str
    content: str
    warnings: List[str]
    errors: List[str]
    stats: Dict[str, Any]
    has_critical_security: bool = False


class AgentsCompiler:
    """Main compiler for generating AGENTS.md files."""
    
    def __init__(self, base_dir: str = "."):
        """Initialize the compiler.
        
        Args:
            base_dir (str): Base directory for compilation. Defaults to current directory.
        """
        self.base_dir = Path(base_dir)
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self._logger = None

    def _log(self, method: str, message: str, **kwargs):
        """Delegate to logger if available, else no-op."""
        if self._logger:
            getattr(self._logger, method)(message, **kwargs)
    
    def compile(self, config: CompilationConfig, primitives: Optional[PrimitiveCollection] = None, logger=None) -> CompilationResult:
        """Compile AGENTS.md and/or CLAUDE.md based on target configuration.
        
        Routes compilation to appropriate targets based on config.target:
        - "vscode" or "agents": Generate AGENTS.md + .github/ structure
        - "claude": Generate CLAUDE.md + .claude/ structure  
        - "all": Generate both targets
        
        Args:
            config (CompilationConfig): Compilation configuration.
            primitives (Optional[PrimitiveCollection]): Primitives to use, or None to discover.
        
        Returns:
            CompilationResult: Result of the compilation.
        """
        self.warnings.clear()
        self.errors.clear()
        self._logger = logger
        
        try:
            # Use provided primitives or discover them (with dependency support)
            if primitives is None:
                if config.local_only:
                    # Use basic discovery for local-only mode
                    primitives = discover_primitives(
                        str(self.base_dir),
                        exclude_patterns=config.exclude,
                    )
                else:
                    # Use enhanced discovery with dependencies (Task 4 integration)
                    from ..primitives.discovery import discover_primitives_with_dependencies
                    primitives = discover_primitives_with_dependencies(
                        str(self.base_dir),
                        exclude_patterns=config.exclude,
                    )
            
            # Route to targets based on config.target.
            # Use target_detection helpers as the single source of truth so
            # new targets (codex, opencode, cursor, minimal, ...) route
            # correctly without touching this method again.
            routing_target = (
                "vscode" if config.target in _VSCODE_TARGET_ALIASES else config.target
            )

            if routing_target not in _KNOWN_TARGETS and config.target not in _KNOWN_TARGETS:
                self.errors.append(
                    f"Unknown compilation target: {config.target!r}. "
                    f"Expected one of: {', '.join(sorted(set(_KNOWN_TARGETS)))}"
                )
                return CompilationResult(
                    success=False,
                    output_path="",
                    content="",
                    warnings=self.warnings.copy(),
                    errors=self.errors.copy(),
                    stats={},
                )

            results: List[CompilationResult] = []

            if should_compile_agents_md(routing_target):
                results.append(self._compile_agents_md(config, primitives))

            if should_compile_claude_md(routing_target):
                results.append(self._compile_claude_md(config, primitives))

            if should_compile_gemini_md(routing_target):
                results.append(self._compile_gemini_md(config, primitives))

            # Some targets (e.g. cursor) use the data-driven
            # integration layer and don't need compilation.
            if not results:
                return CompilationResult(
                    success=True,
                    output_path="",
                    content="",
                    warnings=self.warnings.copy(),
                    errors=self.errors.copy(),
                    stats={},
                )

            # Merge results from all targets
            return self._merge_results(results)
                
        except Exception as e:
            self.errors.append(f"Compilation failed: {str(e)}")
            return CompilationResult(
                success=False,
                output_path="",
                content="",
                warnings=self.warnings.copy(),
                errors=self.errors.copy(),
                stats={}
            )
    
    def _compile_agents_md(self, config: CompilationConfig, primitives: PrimitiveCollection) -> CompilationResult:
        """Compile AGENTS.md files (VSCode/Copilot target).
        
        Args:
            config (CompilationConfig): Compilation configuration.
            primitives (PrimitiveCollection): Primitives to compile.
        
        Returns:
            CompilationResult: Result of the AGENTS.md compilation.
        """
        # Handle distributed compilation (Task 7 - new default behavior)
        if config.strategy == "distributed" and not config.single_agents:
            return self._compile_distributed(config, primitives)
        else:
            # Traditional single-file compilation (backward compatibility)
            return self._compile_single_file(config, primitives)
    
    def _compile_distributed(self, config: CompilationConfig, primitives: PrimitiveCollection) -> CompilationResult:
        """Compile using distributed AGENTS.md approach (Task 7).
        
        Args:
            config (CompilationConfig): Compilation configuration.
            primitives (PrimitiveCollection): Primitives to compile.
        
        Returns:
            CompilationResult: Result of distributed compilation.
        """
        from .distributed_compiler import DistributedAgentsCompiler
        
        errors = self.validate_primitives(primitives)
        self.errors.extend(errors)
        
        # Create distributed compiler with exclude patterns
        distributed_compiler = DistributedAgentsCompiler(
            str(self.base_dir),
            exclude_patterns=config.exclude
        )
        
        # Prepare configuration for distributed compilation
        distributed_config = {
            'min_instructions_per_file': config.min_instructions_per_file,
            # max_depth removed - full project analysis
            'source_attribution': config.source_attribution,
            'debug': config.debug,
            'clean_orphaned': config.clean_orphaned,
            'dry_run': config.dry_run
        }
        
        # Compile distributed
        distributed_result = distributed_compiler.compile_distributed(primitives, distributed_config)
        
        # Display professional compilation output (always show, not just in debug)
        compilation_results = distributed_compiler.get_compilation_results_for_display(config.dry_run)
        if compilation_results:
            if config.debug or config.trace:
                # Verbose mode with mathematical analysis
                output = distributed_compiler.output_formatter.format_verbose(compilation_results)
            elif config.dry_run:
                # Dry run mode with placement preview
                output = distributed_compiler.output_formatter.format_dry_run(compilation_results)
            else:
                # Default mode with essential information
                output = distributed_compiler.output_formatter.format_default(compilation_results)
            
            # Display the professional output
            self._log("progress", output)
        
        if not distributed_result.success:
            self.warnings.extend(distributed_result.warnings)
            self.errors.extend(distributed_result.errors)
            return CompilationResult(
                success=False,
                output_path="",
                content="",
                warnings=self.warnings.copy(),
                errors=self.errors.copy(),
                stats=distributed_result.stats
            )
        
        # Handle dry-run mode (preview placement without writing files)
        if config.dry_run:
            # Count files that would be written (directories that exist)
            successful_writes = 0
            for agents_path in distributed_result.content_map.keys():
                if agents_path.parent.exists():
                    successful_writes += 1
            
            # Update stats with actual files that would be written
            if distributed_result.stats:
                distributed_result.stats["agents_files_generated"] = successful_writes
            
            # Don't write files in preview mode - output already shown above
            return CompilationResult(
                success=True,
                output_path="Preview mode - no files written",
                content=self._generate_placement_summary(distributed_result),
                warnings=self.warnings + distributed_result.warnings,
                errors=self.errors + distributed_result.errors,
                stats=distributed_result.stats
            )
        
        # Write distributed AGENTS.md files
        successful_writes = 0
        total_content_entries = len(distributed_result.content_map)
        
        for agents_path, content in distributed_result.content_map.items():
            try:
                self._write_distributed_file(agents_path, content, config)
                successful_writes += 1
            except OSError as e:
                self.errors.append(f"Failed to write {agents_path}: {str(e)}")
        
        # Update stats with actual files written
        if distributed_result.stats:
            distributed_result.stats["agents_files_generated"] = successful_writes
        
        # Merge warnings and errors
        self.warnings.extend(distributed_result.warnings)
        self.errors.extend(distributed_result.errors)
        
        # Create summary for backward compatibility
        summary_content = self._generate_distributed_summary(distributed_result, config)
        
        return CompilationResult(
            success=len(self.errors) == 0,
            output_path=f"Distributed: {len(distributed_result.placements)} AGENTS.md files",
            content=summary_content,
            warnings=self.warnings.copy(),
            errors=self.errors.copy(),
            stats=distributed_result.stats
        )
    
    def _compile_single_file(self, config: CompilationConfig, primitives: PrimitiveCollection) -> CompilationResult:
        """Compile using traditional single-file approach (backward compatibility).
        
        Args:
            config (CompilationConfig): Compilation configuration.
            primitives (PrimitiveCollection): Primitives to compile.
        
        Returns:
            CompilationResult: Result of single-file compilation.
        """
        # Validate primitives
        validation_errors = self.validate_primitives(primitives)
        if validation_errors:
            self.errors.extend(validation_errors)
        
        # Generate template data
        template_data = self._generate_template_data(primitives, config)
        
        # Generate final output
        content = self.generate_output(template_data, config)
        
        # Write output file (constitution injection handled externally in CLI)
        output_path = str(self.base_dir / config.output_path)
        if not config.dry_run:
            self._write_output_file(output_path, content)
        
        # Compile statistics
        stats = self._compile_stats(primitives, template_data)
        
        return CompilationResult(
            success=len(self.errors) == 0,
            output_path=output_path,
            content=content,
            warnings=self.warnings.copy(),
            errors=self.errors.copy(),
            stats=stats
        )
    
    def _compile_claude_md(self, config: CompilationConfig, primitives: PrimitiveCollection) -> CompilationResult:
        """Compile CLAUDE.md files (Claude Code target).
        
        Uses ClaudeFormatter to generate CLAUDE.md files following Claude's
        Memory format with @import syntax, grouped project standards, and
        workflows section for agents/roles.
        
        Args:
            config (CompilationConfig): Compilation configuration.
            primitives (PrimitiveCollection): Primitives to compile.
        
        Returns:
            CompilationResult: Result of the CLAUDE.md compilation.
        """
        errors = self.validate_primitives(primitives)
        self.errors.extend(errors)
        
        # Create Claude formatter
        claude_formatter = ClaudeFormatter(str(self.base_dir))
        
        # Get placement map from distributed compiler for consistency
        from .distributed_compiler import DistributedAgentsCompiler
        distributed_compiler = DistributedAgentsCompiler(
            str(self.base_dir),
            exclude_patterns=config.exclude
        )
        
        # Analyze directory structure and determine placement
        directory_map = distributed_compiler.analyze_directory_structure(primitives.instructions)
        placement_map = distributed_compiler.determine_agents_placement(
            primitives.instructions,
            directory_map,
            min_instructions=config.min_instructions_per_file,
            debug=config.debug
        )
        
        # Format CLAUDE.md files
        claude_config = {
            'source_attribution': config.source_attribution,
            'debug': config.debug
        }
        claude_result = claude_formatter.format_distributed(primitives, placement_map, claude_config)
        
        # NOTE: Claude commands are now generated at install time via CommandIntegrator,
        # not at compile time. This keeps behavior consistent with VSCode prompt integration.
        
        # Merge warnings and errors (no command result anymore)
        all_warnings = self.warnings + claude_result.warnings
        all_errors = self.errors + claude_result.errors
        
        # Handle dry-run mode
        if config.dry_run:
            # Generate preview summary
            preview_lines = [
                f"CLAUDE.md Preview: Would generate {len(claude_result.placements)} files"
            ]
            for claude_path in claude_result.content_map.keys():
                rel_path = portable_relpath(claude_path, self.base_dir)
                preview_lines.append(f"  {rel_path}")
            
            return CompilationResult(
                success=len(all_errors) == 0,
                output_path="Preview mode - CLAUDE.md",
                content="\n".join(preview_lines),
                warnings=all_warnings,
                errors=all_errors,
                stats=claude_result.stats
            )
        
        # Write CLAUDE.md files
        files_written = 0
        critical_security_found = False
        from ..security.gate import WARN_POLICY, SecurityGate
        for claude_path, content in claude_result.content_map.items():
            try:
                # Create directory if needed
                claude_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Handle constitution injection if enabled
                final_content = content
                if config.with_constitution:
                    try:
                        from .injector import ConstitutionInjector
                        injector = ConstitutionInjector(str(claude_path.parent))
                        final_content, _, _ = injector.inject(
                            content,
                            with_constitution=True,
                            output_path=claude_path
                        )
                    except Exception as exc:
                        _logger.debug("Constitution injection failed for %s: %s", claude_path, exc)
                
                # Defense-in-depth: scan compiled output before writing
                verdict = SecurityGate.scan_text(
                    final_content, str(claude_path), policy=WARN_POLICY
                )
                actionable = verdict.critical_count + verdict.warning_count
                if actionable:
                    if verdict.has_critical:
                        critical_security_found = True
                    all_warnings.append(
                        f"CLAUDE.md contains {actionable} hidden character(s) "
                        f"— run 'apm audit --file {claude_path}' to inspect"
                    )

                claude_path.write_text(final_content, encoding='utf-8')
                files_written += 1
            except OSError as e:
                all_errors.append(f"Failed to write {claude_path}: {str(e)}")
        
        # Update stats
        stats = claude_result.stats.copy()
        stats['claude_files_written'] = files_written
        
        # Display CLAUDE.md compilation output using standard formatter
        # Get proper compilation results from distributed compiler (has optimization decisions)
        from ..output.formatters import CompilationFormatter
        from ..output.models import CompilationResults
        
        compilation_results = distributed_compiler.get_compilation_results_for_display(is_dry_run=config.dry_run)
        if compilation_results:
            # Update target name for CLAUDE.md output
            formatter_results = CompilationResults(
                project_analysis=compilation_results.project_analysis,
                optimization_decisions=compilation_results.optimization_decisions,
                placement_summaries=compilation_results.placement_summaries,
                optimization_stats=compilation_results.optimization_stats,
                warnings=all_warnings,
                errors=all_errors,
                is_dry_run=config.dry_run,
                target_name="CLAUDE.md"
            )
            
            # Use the same formatter as AGENTS.md
            formatter = CompilationFormatter(use_color=True)
            if config.debug or config.trace:
                output = formatter.format_verbose(formatter_results)
            elif config.dry_run:
                output = formatter.format_dry_run(formatter_results)
            else:
                output = formatter.format_default(formatter_results)
            self._log("progress", output)
        
        # Generate summary content for result object
        summary_lines = [
            f"# CLAUDE.md Compilation Summary",
            f"",
            f"Generated {files_written} CLAUDE.md files:"
        ]
        for placement in claude_result.placements:
            rel_path = portable_relpath(placement.claude_path, self.base_dir)
            summary_lines.append(f"- {rel_path} ({len(placement.instructions)} instructions)")
        
        return CompilationResult(
            success=len(all_errors) == 0,
            output_path=f"CLAUDE.md: {files_written} files",
            content="\n".join(summary_lines),
            warnings=all_warnings,
            errors=all_errors,
            stats=stats,
            has_critical_security=critical_security_found,
        )

    def _compile_gemini_md(self, config: CompilationConfig, primitives: PrimitiveCollection) -> CompilationResult:
        """Compile GEMINI.md stub that imports AGENTS.md.

        Gemini CLI supports ``@./path`` import syntax, so GEMINI.md is a
        thin wrapper that pulls in AGENTS.md at load time.  The actual
        instruction roll-up is handled by the AGENTS.md pipeline (which
        is always compiled alongside via ``should_compile_agents_md``).

        Args:
            config: Compilation configuration.
            primitives: Primitives to compile.

        Returns:
            CompilationResult for the GEMINI.md compilation.
        """
        from .gemini_formatter import GeminiFormatter

        gemini_formatter = GeminiFormatter(str(self.base_dir))
        gemini_result = gemini_formatter.format_distributed(primitives)

        all_warnings = self.warnings + gemini_result.warnings
        all_errors = self.errors + gemini_result.errors

        if config.dry_run:
            return CompilationResult(
                success=len(all_errors) == 0,
                output_path="Preview mode - GEMINI.md",
                content="GEMINI.md Preview: Would generate stub importing AGENTS.md",
                warnings=all_warnings,
                errors=all_errors,
                stats=gemini_result.stats,
            )

        files_written = 0
        for gemini_path, content in gemini_result.content_map.items():
            try:
                gemini_path.parent.mkdir(parents=True, exist_ok=True)
                gemini_path.write_text(content, encoding="utf-8")
                files_written += 1
            except OSError as e:
                all_errors.append(f"Failed to write {gemini_path}: {str(e)}")

        stats = gemini_result.stats.copy()
        stats["gemini_files_written"] = files_written

        self._log("progress", "Generated GEMINI.md (imports AGENTS.md)")

        return CompilationResult(
            success=len(all_errors) == 0,
            output_path=f"GEMINI.md: {files_written} files",
            content=f"Generated {files_written} GEMINI.md stub importing AGENTS.md",
            warnings=all_warnings,
            errors=all_errors,
            stats=stats,
        )

    def _merge_results(self, results: List[CompilationResult]) -> CompilationResult:
        """Merge multiple compilation results into a single result.
        
        Combines warnings, errors, stats, and content from multiple target
        compilations into a unified result.
        
        Args:
            results (List[CompilationResult]): List of compilation results to merge.
        
        Returns:
            CompilationResult: Merged compilation result.
        """
        if not results:
            return CompilationResult(
                success=True,
                output_path="",
                content="",
                warnings=[],
                errors=[],
                stats={}
            )
        
        if len(results) == 1:
            return results[0]
        
        # Merge all results
        merged_warnings: List[str] = []
        merged_errors: List[str] = []
        merged_stats: Dict[str, Any] = {}
        output_paths: List[str] = []
        content_parts: List[str] = []
        
        for result in results:
            merged_warnings.extend(result.warnings)
            merged_errors.extend(result.errors)
            
            # Merge stats with prefixes to avoid collisions
            for key, value in result.stats.items():
                if key in merged_stats:
                    # Handle numeric stats by summing
                    if isinstance(value, (int, float)) and isinstance(merged_stats[key], (int, float)):
                        merged_stats[key] = merged_stats[key] + value
                else:
                    merged_stats[key] = value
            
            if result.output_path:
                output_paths.append(result.output_path)
            if result.content:
                content_parts.append(result.content)
        
        # Determine overall success
        overall_success = all(r.success for r in results)
        
        return CompilationResult(
            success=overall_success,
            output_path=" | ".join(output_paths) if output_paths else "",
            content="\n\n---\n\n".join(content_parts) if content_parts else "",
            warnings=merged_warnings,
            errors=merged_errors,
            stats=merged_stats,
            has_critical_security=any(r.has_critical_security for r in results),
        )
    
    def validate_primitives(self, primitives: PrimitiveCollection) -> List[str]:
        """Validate primitives for compilation.
        
        Args:
            primitives (PrimitiveCollection): Collection of primitives to validate.
        
        Returns:
            List[str]: List of validation errors.
        """
        errors = []
        
        # Validate each primitive
        for primitive in primitives.all_primitives():
            primitive_errors = primitive.validate()
            if primitive_errors:
                file_path = portable_relpath(primitive.file_path, self.base_dir)
                
                for error in primitive_errors:
                    # Treat validation errors as warnings instead of hard errors
                    # This allows compilation to continue with incomplete primitives
                    self.warnings.append(f"{file_path}: {error}")
            
            # Validate markdown links in each primitive's content using its own directory as base
            if hasattr(primitive, 'content') and primitive.content:
                primitive_dir = primitive.file_path.parent
                link_errors = validate_link_targets(primitive.content, primitive_dir)
                if link_errors:
                    file_path = portable_relpath(primitive.file_path, self.base_dir)
                    
                    for link_error in link_errors:
                        self.warnings.append(f"{file_path}: {link_error}")
        
        return errors
    
    def generate_output(self, template_data: TemplateData, config: CompilationConfig) -> str:
        """Generate the final AGENTS.md output.
        
        Args:
            template_data (TemplateData): Data for template generation.
            config (CompilationConfig): Compilation configuration.
        
        Returns:
            str: Generated AGENTS.md content.
        """
        content = generate_agents_md_template(template_data)
        
        # Resolve markdown links if enabled
        if config.resolve_links:
            content = resolve_markdown_links(content, self.base_dir)
        
        return content
    
    def _generate_template_data(self, primitives: PrimitiveCollection, config: CompilationConfig) -> TemplateData:
        """Generate template data from primitives and configuration.
        
        Args:
            primitives (PrimitiveCollection): Discovered primitives.
            config (CompilationConfig): Compilation configuration.
        
        Returns:
            TemplateData: Template data for generation.
        """
        # Build instructions content
        instructions_content = build_conditional_sections(primitives.instructions)

        # Metadata (version only; timestamp intentionally omitted for determinism)
        version = get_version()

        # Handle chatmode content
        chatmode_content = None
        if config.chatmode:
            chatmode = find_chatmode_by_name(primitives.chatmodes, config.chatmode)
            if chatmode:
                chatmode_content = chatmode.content
            else:
                self.warnings.append(f"Chatmode '{config.chatmode}' not found")

        return TemplateData(
            instructions_content=instructions_content,
            version=version,
            chatmode_content=chatmode_content
        )
    
    def _write_output_file(self, output_path: str, content: str) -> None:
        """Write the generated content to the output file.
        
        Args:
            output_path (str): Path to write the output.
            content (str): Content to write.
        """
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)
        except OSError as e:
            self.errors.append(f"Failed to write output file {output_path}: {str(e)}")
    
    def _compile_stats(self, primitives: PrimitiveCollection, template_data: TemplateData) -> Dict[str, Any]:
        """Compile statistics about the compilation.
        
        Args:
            primitives (PrimitiveCollection): Discovered primitives.
            template_data (TemplateData): Generated template data.
        
        Returns:
            Dict[str, Any]: Compilation statistics.
        """
        return {
            "primitives_found": primitives.count(),
            "chatmodes": len(primitives.chatmodes),
            "instructions": len(primitives.instructions),
            "contexts": len(primitives.contexts),
            "content_length": len(template_data.instructions_content),
            # timestamp removed
            "version": template_data.version
        }


    def _write_distributed_file(self, agents_path: Path, content: str, config: CompilationConfig) -> None:
        """Write a distributed AGENTS.md file with constitution injection support.
        
        Args:
            agents_path (Path): Path to write the AGENTS.md file.
            content (str): Content to write.
            config (CompilationConfig): Compilation configuration.
        """
        try:
            # Handle constitution injection for distributed files
            final_content = content
            
            if config.with_constitution:
                # Try to inject constitution if available
                try:
                    from .injector import ConstitutionInjector
                    injector = ConstitutionInjector(str(agents_path.parent))
                    final_content, c_status, c_hash = injector.inject(
                        content, 
                        with_constitution=True, 
                        output_path=agents_path
                    )
                except Exception as exc:
                    _logger.debug("Constitution injection failed for %s: %s", agents_path, exc)
            
            # Create directory if it doesn't exist
            agents_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write the file
            with open(agents_path, 'w', encoding='utf-8') as f:
                f.write(final_content)
                
        except OSError as e:
            raise OSError(f"Failed to write distributed AGENTS.md file {agents_path}: {str(e)}")
    
    def _display_placement_preview(self, distributed_result) -> None:
        """Display placement preview for --show-placement mode.
        
        Args:
            distributed_result: Result from distributed compilation.
        """
        self._log("progress", "Distributed AGENTS.md Placement Preview:")
        self._log("progress", "")
        
        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            self._log("verbose_detail", f"{rel_path}")
            self._log("verbose_detail", f"   Instructions: {len(placement.instructions)}")
            self._log("verbose_detail", f"   Patterns: {', '.join(sorted(placement.coverage_patterns))}")
            if placement.source_attribution:
                sources = set(placement.source_attribution.values())
                self._log("verbose_detail", f"   Sources: {', '.join(sorted(sources))}")
            self._log("verbose_detail", "")
    
    def _display_trace_info(self, distributed_result, primitives: PrimitiveCollection) -> None:
        """Display detailed trace information for --trace mode.
        
        Args:
            distributed_result: Result from distributed compilation.
            primitives (PrimitiveCollection): Full primitive collection.
        """
        self._log("progress", "Distributed Compilation Trace:")
        self._log("progress", "")
        
        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            self._log("verbose_detail", f"{rel_path}")
            
            for instruction in placement.instructions:
                source = getattr(instruction, 'source', 'local')
                inst_path = portable_relpath(instruction.file_path, self.base_dir)
                
                self._log("verbose_detail", f"   * {instruction.apply_to or 'no pattern'} <- {source} {inst_path}")
            self._log("verbose_detail", "")
    
    def _generate_placement_summary(self, distributed_result) -> str:
        """Generate a text summary of placement results.
        
        Args:
            distributed_result: Result from distributed compilation.
        
        Returns:
            str: Text summary of placements.
        """
        lines = ["Distributed AGENTS.md Placement Summary:", ""]
        
        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            lines.append(f"{rel_path}")
            lines.append(f"   Instructions: {len(placement.instructions)}")
            lines.append(f"   Patterns: {', '.join(sorted(placement.coverage_patterns))}")
            lines.append("")
        
        lines.append(f"Total AGENTS.md files: {len(distributed_result.placements)}")
        return "\n".join(lines)
    
    def _generate_distributed_summary(self, distributed_result, config: CompilationConfig) -> str:
        """Generate a summary of distributed compilation results.
        
        Args:
            distributed_result: Result from distributed compilation.
            config (CompilationConfig): Compilation configuration.
        
        Returns:
            str: Summary content.
        """
        lines = [
            "# Distributed AGENTS.md Compilation Summary",
            "",
            f"Generated {len(distributed_result.placements)} AGENTS.md files:",
            ""
        ]
        
        for placement in distributed_result.placements:
            rel_path = portable_relpath(placement.agents_path, self.base_dir)
            lines.append(f"- {rel_path} ({len(placement.instructions)} instructions)")
        
        lines.extend([
            "",
            f"Total instructions: {distributed_result.stats.get('total_instructions_placed', 0)}",
            f"Total patterns: {distributed_result.stats.get('total_patterns_covered', 0)}",
            "",
            "Use 'apm compile --single-agents' for traditional single-file compilation."
        ])
        
        return "\n".join(lines)


def compile_agents_md(
    primitives: Optional[PrimitiveCollection] = None,
    output_path: str = "AGENTS.md",
    chatmode: Optional[str] = None,
    dry_run: bool = False,
    base_dir: str = "."
) -> str:
    """Generate AGENTS.md with conditional sections.
    
    Args:
        primitives (Optional[PrimitiveCollection]): Primitives to use, or None to discover.
        output_path (str): Output file path. Defaults to "AGENTS.md".
        chatmode (str): Specific chatmode to use, or None for default.
        dry_run (bool): If True, don't write output file. Defaults to False.
        base_dir (str): Base directory for compilation. Defaults to current directory.
    
    Returns:
        str: Generated AGENTS.md content.
    """
    # Create configuration - use single-file mode for backward compatibility
    config = CompilationConfig(
        output_path=output_path,
        chatmode=chatmode,
        dry_run=dry_run,
        strategy="single-file"  # Force single-file mode for backward compatibility
    )
    
    # Create compiler and compile
    compiler = AgentsCompiler(base_dir)
    result = compiler.compile(config, primitives)
    
    if not result.success:
        raise RuntimeError(f"Compilation failed: {'; '.join(result.errors)}")
    
    return result.content