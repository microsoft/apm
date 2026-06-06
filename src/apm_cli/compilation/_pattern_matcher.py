"""Mixin: pattern-matching and helper methods for ContextOptimizer.

Extracted from context_optimizer.ContextOptimizer to stay under the 800-line
guardrail (Strangler Stage 2 / issue #1078).

Rule B routing
--------------
``Path`` is patched at ``apm_cli.compilation.context_optimizer.Path`` in tests
(specifically ``Path.resolve``).  ``_file_matches_pattern`` constructs
``Path(match)`` objects; it does so via a function-level late import:

    from apm_cli.compilation import context_optimizer as _co
    _co.Path(...)
"""

from __future__ import annotations

import builtins
import fnmatch
import os

from ..utils.paths import portable_relpath
from ..utils.patterns import has_top_level_comma, parse_apply_to


class _PatternMatcherMixin:
    """Mixin: pattern-matching, inheritance-chain, and distribution-score helpers."""

    def _extract_intended_directory_from_pattern(self, pattern: str):
        """Extract the intended directory from a pattern like 'docs/**/*.md' -> 'docs'.

        Args:
            pattern (str): File pattern (may be a comma-separated list).

        Returns:
            Optional[Path]: Intended directory path, or None if pattern is global.
        """
        # For comma-lists, only the first segment is consulted - the
        # placement still flows into a single directory.
        if has_top_level_comma(pattern):
            segments = parse_apply_to(pattern)
            if not segments:
                return None
            pattern = segments[0]

        if not pattern or pattern.startswith("**/"):
            return None  # Global pattern

        if "/" in pattern:
            # Extract the first directory component
            parts = pattern.split("/")
            first_part = parts[0]

            # Skip if it's a wildcard
            if "*" not in first_part and first_part:
                intended_dir = self.base_dir / first_part
                if intended_dir.exists() and intended_dir.is_dir():
                    return intended_dir

        return None

    def _expand_glob_pattern(self, pattern: str) -> builtins.list:
        """Expand glob pattern with brace expansion, supporting multiple brace groups.

        Args:
            pattern (str): Pattern like '**/*.{css,scss}' or '**/*.{test,spec}.{ts,js}'

        Returns:
            List[str]: Expanded patterns like ['**/*.css', '**/*.scss']
                       or ['**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js']
        """
        import re

        # Handle brace expansion like {css,scss}
        brace_match = re.search(r"\{([^}]+)\}", pattern)
        if brace_match:
            alternatives = brace_match.group(1).split(",")
            prefix = pattern[: brace_match.start()]
            suffix = pattern[brace_match.end() :]
            # Recursively expand remaining brace groups in each result
            expanded = []
            for alt in alternatives:
                expanded.extend(self._expand_glob_pattern(prefix + alt + suffix))
            return expanded

        return [pattern]

    def _file_matches_pattern(self, file_path, pattern: str) -> bool:
        """Check if a file matches a given pattern with optimized performance.

        Args:
            file_path (Path): File path to check
            pattern (str): Glob pattern or comma-separated list of globs.

        Returns:
            bool: True if file matches pattern (or any segment of a list).
        """
        # applyTo accepts a comma-separated list of globs; treat any
        # segment match as a hit so list patterns mirror per-glob semantics.
        # Only split on top-level commas - commas inside brace alternation
        # (e.g. ``**/*.{css,scss}``) must stay attached for brace expansion.
        if has_top_level_comma(pattern):
            segments = parse_apply_to(pattern)
            return any(self._file_matches_pattern(file_path, seg) for seg in segments)

        # Expand any brace patterns
        expanded_patterns = self._expand_glob_pattern(pattern)

        for expanded_pattern in expanded_patterns:
            # For patterns with **, use cached glob results
            if "**" in expanded_pattern:
                try:
                    # Resolve both paths to handle symlinks and path inconsistencies
                    resolved_file = file_path.resolve()
                    rel_path = resolved_file.relative_to(self.base_dir.resolve())

                    # Use cached glob results instead of repeated glob calls
                    matches = self._cached_glob(expanded_pattern)
                    # Use cached Set[Path] to avoid recreating on every call
                    if expanded_pattern not in self._glob_set_cache:
                        # Rule B: Path is patched at context_optimizer.Path in tests
                        from apm_cli.compilation import context_optimizer as _co

                        self._glob_set_cache[expanded_pattern] = {
                            _co.Path(match) for match in matches
                        }
                    if rel_path in self._glob_set_cache[expanded_pattern]:
                        return True
                except (ValueError, OSError):
                    pass
            else:
                # For non-recursive patterns, use fnmatch as before
                try:
                    rel_str = portable_relpath(file_path, self.base_dir)
                    if fnmatch.fnmatch(rel_str, expanded_pattern):
                        return True
                except ValueError:
                    pass

                # Only use filename match for patterns without directory structure
                # This prevents "docs/**/*.md" from matching any "*.md" file anywhere
                if "/" not in expanded_pattern:
                    if fnmatch.fnmatch(file_path.name, expanded_pattern):
                        return True

        return False

    def _find_matching_directories(self, pattern: str) -> builtins.set:
        """Find directories that contain files matching the pattern.

        Args:
            pattern (str): File pattern to match.

        Returns:
            Set[Path]: Set of directories with matching files.
        """
        # Use cached result if available
        if pattern in self._pattern_cache:
            return self._pattern_cache[pattern]

        matching_dirs: builtins.set = builtins.set()

        # Use the reliable approach for all patterns
        for directory, analysis in sorted(self._directory_cache.items()):
            try:
                files = [
                    f for f in directory.iterdir() if f.is_file() and not f.name.startswith(".")
                ]

                match_count = 0
                for file_path in files:
                    if self._file_matches_pattern(file_path, pattern):
                        match_count += 1
                        matching_dirs.add(directory)

                if match_count > 0:
                    analysis.pattern_matches[pattern] = match_count
            except (OSError, PermissionError):
                continue

        self._pattern_cache[pattern] = matching_dirs
        return matching_dirs

    def _calculate_inheritance_pollution(self, directory, pattern: str) -> float:
        """Calculate inheritance pollution score for placing instruction at directory.

        Args:
            directory (Path): Candidate placement directory.
            pattern (str): Instruction pattern.

        Returns:
            float: Pollution score (higher = more pollution).
        """
        pollution_score = 0.0

        # Optimization: Only check direct children instead of all directories
        # This prevents O(n2) complexity with unlimited depth analysis
        try:
            direct_children = [
                child
                for child in directory.iterdir()
                if child.is_dir() and child in self._directory_cache
            ]

            # Check only direct child directories for pollution
            for child_dir in direct_children:
                analysis = self._directory_cache[child_dir]

                # If child has no matching files, this creates pollution
                child_relevance = analysis.get_relevance_score(pattern)
                if child_relevance == 0.0:
                    pollution_score += 0.5  # Strong pollution penalty
                elif child_relevance < 0.1:  # Weak relevance threshold
                    pollution_score += 0.2  # Weak pollution penalty
        except (OSError, PermissionError):
            # Skip directories we can't read
            pass

        return pollution_score

    def _calculate_distribution_score(self, matching_directories: builtins.set) -> float:
        """Calculate distribution score with diversity factor.

        Args:
            matching_directories: Set of directories with pattern matches.

        Returns:
            float: Distribution score accounting for spread and depth diversity.
        """
        total_dirs_with_files = len(
            [d for d in self._directory_cache.values() if d.total_files > 0]
        )
        if total_dirs_with_files == 0:
            return 0.0

        base_ratio = len(matching_directories) / total_dirs_with_files

        # Calculate diversity factor based on depth distribution
        depths = [self._directory_cache[d].depth for d in matching_directories]
        if not depths:
            return base_ratio

        depth_variance = sum((d - sum(depths) / len(depths)) ** 2 for d in depths) / len(depths)
        diversity_factor = 1.0 + (depth_variance * self.DIVERSITY_FACTOR_BASE)

        return base_ratio * diversity_factor

    def _get_inheritance_chain(self, working_directory) -> builtins.list:
        """Get inheritance chain from working directory to root.

        Args:
            working_directory (Path): Starting directory.

        Returns:
            List[Path]: Inheritance chain (most specific to root).
        """
        cached = self._inheritance_cache.get(working_directory)
        if cached is not None:
            return cached

        chain = []
        # Resolve the starting directory to ensure consistent path comparison
        try:
            current = working_directory.resolve()
        except (OSError, ValueError):
            current = working_directory.absolute()

        seen_paths = builtins.set()  # Track visited paths to prevent infinite loops

        # Build chain from working directory up to (and including) base_dir
        while current not in seen_paths:
            seen_paths.add(current)
            chain.append(current)

            # Stop at base_dir
            if current == self.base_dir:
                break

            # Stop if we can't go higher or hit filesystem root
            try:
                parent = current.parent
                if parent == current:  # We've hit filesystem root
                    break
                current = parent
            except (OSError, ValueError):
                break

        self._inheritance_cache[working_directory] = chain
        return chain

    def _is_child_directory(self, child, parent) -> bool:
        """Check if child is a subdirectory of parent.

        Args:
            child (Path): Potential child directory.
            parent (Path): Potential parent directory.

        Returns:
            bool: True if child is subdirectory of parent.
        """
        try:
            child.resolve().relative_to(parent.resolve())
            return child.resolve() != parent.resolve()
        except ValueError:
            return False

    def _is_instruction_relevant(self, instruction, working_directory) -> bool:
        """Check if instruction is relevant for the working directory.

        Args:
            instruction (Instruction): Instruction to check.
            working_directory (Path): Directory where agent is working.

        Returns:
            bool: True if instruction is relevant.
        """
        if not instruction.apply_to:
            return True  # Global instructions are always relevant

        pattern = instruction.apply_to

        # Resolve working directory to handle path inconsistencies
        try:
            resolved_working_dir = working_directory.resolve()
        except (OSError, ValueError):
            resolved_working_dir = working_directory.absolute()

        # Check if working directory has files matching the pattern
        analysis = self._directory_cache.get(resolved_working_dir)
        if not analysis:
            return False

        # If pattern already analyzed, use cached result
        if pattern in analysis.pattern_matches:
            return analysis.pattern_matches[pattern] > 0

        # Otherwise, analyze this specific directory for the pattern
        # Only check direct files in this directory (not subdirectories for simplicity)
        matching_files = 0

        try:
            for file in os.listdir(resolved_working_dir):
                if file.startswith("."):
                    continue

                file_path = resolved_working_dir / file
                if file_path.is_file():
                    if self._file_matches_pattern(file_path, pattern):
                        matching_files += 1
        except (OSError, PermissionError):
            # Handle case where directory doesn't exist or can't be read
            pass

        # Cache the result
        analysis.pattern_matches[pattern] = matching_files

        return matching_files > 0
