"""Performance benchmarks for iteration-2 P0 hot paths.

Covers four critical code-paths identified by the Python Architect:

1. ``_match_double_star()`` / ``should_exclude()`` -- recursive glob matcher
2. ``ContentScanner.scan_text()`` -- hidden Unicode character scanning
3. ``ContentScanner.strip_dangerous()`` -- dangerous character stripping
4. ``APMDependencyResolver.build_dependency_tree()`` -- BFS dependency tree

Run with: uv run pytest tests/benchmarks/test_security_and_resolver_benchmarks.py -v -m benchmark
"""

import time
from pathlib import Path
from typing import List

import pytest

from apm_cli.utils.exclude import (
    _match_double_star,
    should_exclude,
    validate_exclude_patterns,
)
from apm_cli.security.content_scanner import ContentScanner, ScanFinding
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.dependency_graph import (
    DependencyTree,
    DependencyNode,
    FlatDependencyMap,
)
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_path_parts(depth: int) -> List[str]:
    """Build path parts like ['a', 'b', 'c', ..., 'test.py'] of given depth."""
    segments = [chr(ord("a") + (i % 26)) for i in range(depth - 1)]
    segments.append("test.py")
    return segments


def _make_double_star_pattern(star_segments: int) -> List[str]:
    """Build pattern parts with N ** segments.

    1 segment:  ['**', 'a', '*.py']
    2 segments: ['**', 'a', '**', 'b', '*.py']
    3 segments: ['**', 'a', '**', 'b', '**', 'c', '*.py']
    """
    parts: List[str] = []
    labels = ["a", "b", "c", "d", "e"]
    for i in range(star_segments):
        parts.append("**")
        parts.append(labels[i % len(labels)])
    parts.append("*.py")
    return parts


def _make_exclude_pattern_str(star_segments: int) -> str:
    """Build a pattern string with N ** segments for should_exclude()."""
    return "/".join(_make_double_star_pattern(star_segments))


def _make_deep_path(depth: int) -> str:
    """Build a forward-slash path string of given depth ending in test.py."""
    return "/".join(_make_path_parts(depth))


def _generate_mixed_content(size: int) -> str:
    """Generate content of approximately *size* characters with non-ASCII chars.

    Mixes ASCII text with zero-width spaces (U+200B) and other suspicious
    characters so that the isascii() fast path is NOT taken.
    """
    # Use a repeating block with embedded non-ASCII chars
    block = "Hello world. " * 5 + "\u200b" + "More text. " * 5 + "\u200c"
    # block is ~130 chars
    repeats = max(1, size // len(block))
    content = (block * repeats)[:size]
    return content


def _generate_ascii_content(size: int) -> str:
    """Generate pure-ASCII content of approximately *size* characters."""
    block = "The quick brown fox jumps over the lazy dog. "
    repeats = max(1, size // len(block))
    return (block * repeats)[:size]


def _generate_dangerous_content(size: int) -> str:
    """Generate content with critical/warning-level dangerous characters.

    Includes tag characters (U+E0001-U+E007F), bidi overrides, and
    zero-width chars that should be stripped by strip_dangerous().
    """
    # Mix of tag characters, bidi overrides, zero-width chars, and ASCII text
    dangerous_chars = [
        "\U000E0041",  # tag character 'A' (critical)
        "\U000E0042",  # tag character 'B' (critical)
        "\u202A",      # LRE bidi override (critical)
        "\u202E",      # RLO bidi override (critical)
        "\u200B",      # zero-width space (warning)
        "\u200D",      # zero-width joiner (warning -- not in emoji context)
        "\u2060",      # word joiner (warning)
    ]
    block = "Normal text here. "
    parts: List[str] = []
    idx = 0
    while len("".join(parts)) < size:
        parts.append(block)
        parts.append(dangerous_chars[idx % len(dangerous_chars)])
        idx += 1
    return "".join(parts)[:size]


def _write_fake_apm_yml(path: Path, deps: List[str]) -> Path:
    """Write an apm.yml with the given dependency list and return its path."""
    lines = [
        "name: bench-root",
        "version: 1.0.0",
    ]
    if deps:
        lines.append("dependencies:")
        lines.append("  apm:")
        for dep in deps:
            lines.append(f"    - {dep}")
    apm_yml = path / "apm.yml"
    apm_yml.write_text("\n".join(lines) + "\n")
    return apm_yml


def _setup_linear_chain(
    tmp_path: Path, length: int
) -> Path:
    """Create a linear dependency chain: root -> pkg-0 -> pkg-1 -> ... -> pkg-(length-1).

    Each package has an apm.yml pointing to the next package in the chain.
    Returns the root apm.yml path.
    """
    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()

    # Create dependency packages, each pointing to the next
    for i in range(length):
        owner_dir = apm_modules / "org"
        owner_dir.mkdir(exist_ok=True)
        pkg_dir = owner_dir / f"pkg-{i}"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        if i < length - 1:
            next_dep = [f"org/pkg-{i + 1}"]
        else:
            next_dep = []
        _write_fake_apm_yml(pkg_dir, next_dep)

    # Create root apm.yml
    root_deps = ["org/pkg-0"] if length > 0 else []
    return _write_fake_apm_yml(tmp_path, root_deps)


def _setup_wide_fan(
    tmp_path: Path, breadth: int
) -> Path:
    """Create a wide fan: root -> [pkg-0, pkg-1, ..., pkg-(breadth-1)].

    Each leaf package has no further dependencies.
    Returns the root apm.yml path.
    """
    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()

    for i in range(breadth):
        owner_dir = apm_modules / "org"
        owner_dir.mkdir(exist_ok=True)
        pkg_dir = owner_dir / f"pkg-{i}"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        _write_fake_apm_yml(pkg_dir, [])

    root_deps = [f"org/pkg-{i}" for i in range(breadth)]
    return _write_fake_apm_yml(tmp_path, root_deps)


def _setup_diamond(tmp_path: Path) -> Path:
    """Create a diamond dependency graph:

        root -> A, B
        A -> C
        B -> C  (shared transitive dep)

    Returns the root apm.yml path.
    """
    apm_modules = tmp_path / "apm_modules"
    apm_modules.mkdir()

    owner = apm_modules / "org"
    owner.mkdir()

    # C has no deps
    (owner / "c").mkdir()
    _write_fake_apm_yml(owner / "c", [])

    # A depends on C
    (owner / "a").mkdir()
    _write_fake_apm_yml(owner / "a", ["org/c"])

    # B depends on C
    (owner / "b").mkdir()
    _write_fake_apm_yml(owner / "b", ["org/c"])

    # Root depends on A and B
    return _write_fake_apm_yml(tmp_path, ["org/a", "org/b"])


# ---------------------------------------------------------------------------
# P0 #1: _match_double_star() / should_exclude()
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestDoubleStarThroughput:
    """Benchmark _match_double_star() with varying ** segments and path depth."""

    @pytest.mark.parametrize(
        "star_segments, path_depth",
        [
            (1, 5),
            (1, 10),
            (1, 20),
            (2, 5),
            (2, 10),
            (2, 20),
            (3, 5),
            (3, 10),
            (3, 20),
        ],
    )
    def test_double_star_throughput(
        self, star_segments: int, path_depth: int
    ):
        """_match_double_star with N ** segments on depth-D path stays under 2s."""
        path_parts = _make_path_parts(path_depth)
        pattern_parts = _make_double_star_pattern(star_segments)

        start = time.perf_counter()
        result = _match_double_star(path_parts, pattern_parts)
        elapsed = time.perf_counter() - start

        # We don't require a specific match result; just that it completes
        assert isinstance(result, bool)
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < 10.0, (
            f"_match_double_star({star_segments} ** segs, depth {path_depth}) "
            f"took {elapsed:.3f}s, expected < 10.0s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestDoubleStarFastPath:
    """Verify non-** patterns are significantly faster than ** patterns."""

    def test_simple_glob_fast_path(self):
        """Patterns without ** (e.g., '*.py') should be near-instant."""
        from apm_cli.utils.exclude import _matches_pattern

        path_str = "src/module/deep/nested/file.py"

        # Simple glob -- no ** recursion
        start = time.perf_counter()
        for _ in range(1000):
            _matches_pattern(path_str, "*.py")
        elapsed_simple = time.perf_counter() - start

        # ** glob -- recursion
        start = time.perf_counter()
        for _ in range(1000):
            _matches_pattern(path_str, "**/*.py")
        elapsed_double_star = time.perf_counter() - start

        # Both should be fast, but simple should be noticeably faster
        # Generous ceilings (5x expected) -- catches catastrophic regressions only.
        assert elapsed_simple < 2.5, (
            f"Simple glob took {elapsed_simple:.3f}s for 1000 calls, "
            f"expected < 2.5s (generous ceiling)"
        )
        assert elapsed_double_star < 5.0, (
            f"** glob took {elapsed_double_star:.3f}s for 1000 calls, "
            f"expected < 5.0s (generous ceiling)"
        )
        # Fast-path should be faster than recursive ** matching
        if elapsed_double_star > 0.001:
            assert elapsed_simple < elapsed_double_star, (
                f"simple glob ({elapsed_simple:.4f}s) should be faster "
                f"than ** glob ({elapsed_double_star:.4f}s)"
            )

    def test_non_star_patterns_fast(self):
        """Non-** patterns like 'test_*.md' should match via fnmatch fast path."""
        from apm_cli.utils.exclude import _matches_pattern

        start = time.perf_counter()
        for _ in range(1000):
            _matches_pattern("test_example.md", "test_*.md")
        elapsed = time.perf_counter() - start

        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        assert elapsed < 2.5, (
            f"fnmatch pattern took {elapsed:.3f}s for 1000 calls, "
            f"expected < 2.5s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestDoubleStarCorrectness:
    """Verify correctness of _match_double_star for known patterns."""

    def test_one_double_star_segment_matches(self):
        """'**' + 'a' + '*.py' should match paths containing 'a' before .py."""
        # Should match: path has 'a' segment followed by a .py file
        assert _match_double_star(
            ["src", "a", "test.py"], ["**", "a", "*.py"]
        ) is True

    def test_one_double_star_segment_no_match(self):
        """Pattern should NOT match when required segment is absent."""
        assert _match_double_star(
            ["src", "b", "test.py"], ["**", "a", "*.py"]
        ) is False

    def test_double_star_matches_zero_dirs(self):
        """** can match zero directories."""
        assert _match_double_star(
            ["a", "test.py"], ["**", "a", "*.py"]
        ) is True

    def test_double_star_matches_multiple_dirs(self):
        """** can match multiple directories."""
        assert _match_double_star(
            ["x", "y", "z", "a", "test.py"], ["**", "a", "*.py"]
        ) is True

    def test_two_star_segments(self):
        """Pattern with 2 ** segments matches nested structure."""
        assert _match_double_star(
            ["x", "a", "y", "z", "b", "test.py"],
            ["**", "a", "**", "b", "*.py"],
        ) is True

    def test_two_star_segments_no_match(self):
        """Two ** segments fail when second anchor is missing."""
        assert _match_double_star(
            ["x", "a", "y", "z", "test.py"],
            ["**", "a", "**", "b", "*.py"],
        ) is False

    def test_wrong_extension_no_match(self):
        """*.py pattern should not match .txt files."""
        assert _match_double_star(
            ["a", "test.txt"], ["**", "a", "*.py"]
        ) is False

    def test_should_exclude_integration(self, tmp_path: Path):
        """should_exclude() correctly uses _match_double_star via _matches_pattern."""
        # Create a real file so path resolution works
        test_file = tmp_path / "src" / "utils" / "helper.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# helper\n")

        patterns = validate_exclude_patterns(["**/utils/*.py"])
        assert should_exclude(test_file, tmp_path, patterns) is True

        non_match = tmp_path / "src" / "core" / "main.py"
        non_match.parent.mkdir(parents=True, exist_ok=True)
        non_match.write_text("# main\n")
        assert should_exclude(non_match, tmp_path, patterns) is False


# ---------------------------------------------------------------------------
# P0 #2: ContentScanner.scan_text()
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestScanTextThroughput:
    """Benchmark ContentScanner.scan_text() across content sizes."""

    @pytest.mark.parametrize(
        "content_size, ceiling",
        [
            (1_000, 2.5),
            (10_000, 10.0),
            (100_000, 50.0),
        ],
    )
    def test_scan_text_mixed_content(self, content_size: int, ceiling: float):
        """scan_text() on non-ASCII content of size N completes within ceiling."""
        content = _generate_mixed_content(content_size)
        # Verify it's truly non-ASCII so we exercise the character loop
        assert not content.isascii(), "Content should be non-ASCII"

        start = time.perf_counter()
        findings = ContentScanner.scan_text(content, filename="bench.md")
        elapsed = time.perf_counter() - start

        assert isinstance(findings, list)
        # Non-ASCII mixed content should produce findings
        assert len(findings) > 0
        assert any(f.severity in ("warning", "critical") for f in findings), (
            "Expected at least one warning or critical finding from mixed content"
        )
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < ceiling, (
            f"scan_text({content_size} chars) took {elapsed:.3f}s, "
            f"expected < {ceiling}s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestScanTextFastPath:
    """Verify isascii() fast path makes pure-ASCII scanning near-instant."""

    def test_ascii_fast_path(self):
        """Pure ASCII content should trigger isascii() short-circuit."""
        content = _generate_ascii_content(100_000)
        assert content.isascii(), "Content must be pure ASCII for this test"

        start = time.perf_counter()
        findings = ContentScanner.scan_text(content, filename="ascii.md")
        elapsed = time.perf_counter() - start

        assert findings == []
        # Generous ceiling -- catches catastrophic regressions only.
        assert elapsed < 2.0, (
            f"ASCII fast path took {elapsed:.6f}s for 100K chars, "
            f"expected < 2.0s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestScanTextCorrectness:
    """Verify scan_text returns correct ScanFinding objects."""

    def test_zero_width_space_detected(self):
        """Zero-width space (U+200B) should be detected as warning."""
        content = "Hello\u200bWorld"
        findings = ContentScanner.scan_text(content, filename="test.md")

        assert len(findings) == 1
        assert isinstance(findings[0], ScanFinding)
        assert findings[0].codepoint == "U+200B"
        assert findings[0].severity == "warning"
        assert findings[0].category == "zero-width"

    def test_tag_character_detected_as_critical(self):
        """Tag characters (U+E0041) should be detected as critical."""
        content = "Normal\U000E0041text"
        findings = ContentScanner.scan_text(content, filename="test.md")

        critical = [f for f in findings if f.severity == "critical"]
        assert len(critical) >= 1
        assert critical[0].category == "tag-character"

    def test_bidi_override_detected(self):
        """Bidi override (U+202E RLO) should be critical."""
        content = "Hello\u202Eworld"
        findings = ContentScanner.scan_text(content, filename="test.md")

        critical = [f for f in findings if f.severity == "critical"]
        assert len(critical) >= 1
        assert critical[0].category == "bidi-override"

    def test_line_and_column_positions(self):
        """Findings should report correct 1-based line and column."""
        # Put a zero-width space at line 2, column 6
        content = "Line one\nHello\u200bWorld"
        findings = ContentScanner.scan_text(content, filename="test.md")

        assert len(findings) == 1
        assert findings[0].line == 2
        assert findings[0].column == 6

    def test_empty_content_returns_empty(self):
        """Empty string should return no findings."""
        assert ContentScanner.scan_text("") == []


# ---------------------------------------------------------------------------
# P0 #3: ContentScanner.strip_dangerous()
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestStripDangerousThroughput:
    """Benchmark ContentScanner.strip_dangerous() across content sizes."""

    @pytest.mark.parametrize(
        "content_size, ceiling",
        [
            (1_000, 2.5),
            (10_000, 10.0),
            (100_000, 50.0),
        ],
    )
    def test_strip_dangerous_throughput(
        self, content_size: int, ceiling: float
    ):
        """strip_dangerous() on dangerous content of size N within ceiling."""
        content = _generate_dangerous_content(content_size)

        start = time.perf_counter()
        result = ContentScanner.strip_dangerous(content)
        elapsed = time.perf_counter() - start

        assert isinstance(result, str)
        # Result should be shorter (dangerous chars removed)
        assert len(result) <= len(content)
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        assert elapsed < ceiling, (
            f"strip_dangerous({content_size} chars) took {elapsed:.3f}s, "
            f"expected < {ceiling}s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestStripDangerousCorrectness:
    """Verify strip_dangerous removes dangerous chars and preserves safe ones."""

    def test_critical_chars_removed(self):
        """Tag characters and bidi overrides should be stripped."""
        content = "Hello\U000E0041\U000E0042\u202EWorld"
        result = ContentScanner.strip_dangerous(content)

        # Verify dangerous chars are gone
        assert "\U000E0041" not in result
        assert "\U000E0042" not in result
        assert "\u202E" not in result
        # ASCII text should be preserved
        assert "Hello" in result
        assert "World" in result

    def test_warning_chars_removed(self):
        """Warning-level chars (zero-width space, ZWNJ) should be stripped."""
        content = "Hello\u200b\u200cWorld"
        result = ContentScanner.strip_dangerous(content)

        assert "\u200b" not in result
        assert "\u200c" not in result
        assert result == "HelloWorld"

    def test_info_chars_preserved(self):
        """Info-level chars (non-breaking space, emoji selector) should be kept."""
        content = "Hello\u00a0World"  # non-breaking space is info-level
        result = ContentScanner.strip_dangerous(content)

        assert "\u00a0" in result
        assert result == content

    def test_pure_ascii_unchanged(self):
        """Pure ASCII content should pass through unchanged."""
        content = "Hello World! This is normal text."
        result = ContentScanner.strip_dangerous(content)
        assert result == content

    def test_stripped_content_has_no_dangerous_chars(self):
        """After stripping, re-scanning should find no critical/warning findings."""
        content = _generate_dangerous_content(1_000)
        result = ContentScanner.strip_dangerous(content)

        findings = ContentScanner.scan_text(result, filename="stripped.md")
        dangerous = [
            f for f in findings if f.severity in ("critical", "warning")
        ]
        assert len(dangerous) == 0, (
            f"Stripped content still has {len(dangerous)} dangerous findings"
        )


@pytest.mark.benchmark
class TestStripDangerousIdempotency:
    """Verify strip_dangerous is idempotent."""

    def test_idempotent(self):
        """strip_dangerous(strip_dangerous(x)) == strip_dangerous(x)."""
        content = _generate_dangerous_content(5_000)
        first_pass = ContentScanner.strip_dangerous(content)
        second_pass = ContentScanner.strip_dangerous(first_pass)

        assert second_pass == first_pass, (
            "strip_dangerous is not idempotent: second pass changed the output"
        )

    def test_idempotent_with_mixed_severities(self):
        """Idempotency holds even with mixed critical/warning/info content."""
        # Include info-level chars that should be preserved
        content = (
            "Hello\U000E0041World\u200b"  # critical + warning
            "\u00a0normal\u202E"           # info + critical
            "end"
        )
        first_pass = ContentScanner.strip_dangerous(content)
        second_pass = ContentScanner.strip_dangerous(first_pass)
        assert second_pass == first_pass


# ---------------------------------------------------------------------------
# P0 #4: APMDependencyResolver.build_dependency_tree()
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestBuildDependencyTreeShapes:
    """Benchmark build_dependency_tree() across graph shapes."""

    def test_linear_chain(self, tmp_path: Path):
        """Linear chain: depth=50, breadth=1 -- BFS through a long chain."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _setup_linear_chain(tmp_path, 50)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        start = time.perf_counter()
        tree = resolver.build_dependency_tree(root_yml)
        elapsed = time.perf_counter() - start

        assert isinstance(tree, DependencyTree)
        # Should have all 50 packages in the chain
        assert len(tree.nodes) == 50
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        assert elapsed < 25.0, (
            f"Linear chain (50 nodes) took {elapsed:.3f}s, "
            f"expected < 25.0s (generous ceiling)"
        )

    def test_wide_fan(self, tmp_path: Path):
        """Wide fan: depth=1, breadth=50 -- many direct dependencies."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _setup_wide_fan(tmp_path, 50)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        start = time.perf_counter()
        tree = resolver.build_dependency_tree(root_yml)
        elapsed = time.perf_counter() - start

        assert isinstance(tree, DependencyTree)
        assert len(tree.nodes) == 50
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        assert elapsed < 25.0, (
            f"Wide fan (50 nodes) took {elapsed:.3f}s, "
            f"expected < 25.0s (generous ceiling)"
        )

    def test_diamond_deduplication(self, tmp_path: Path):
        """Diamond: shared transitive dep C should not be duplicated."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _setup_diamond(tmp_path)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        start = time.perf_counter()
        tree = resolver.build_dependency_tree(root_yml)
        elapsed = time.perf_counter() - start

        assert isinstance(tree, DependencyTree)
        # Diamond: A, B, C = 3 unique nodes (C is shared, not duplicated)
        assert len(tree.nodes) == 3, (
            f"Diamond should have 3 unique nodes, got {len(tree.nodes)}: "
            f"{list(tree.nodes.keys())}"
        )
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        assert elapsed < 10.0, (
            f"Diamond took {elapsed:.3f}s, expected < 10.0s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestBuildDependencyTreeScale:
    """Benchmark build_dependency_tree() at various scales."""

    @pytest.mark.parametrize("node_count", [10, 50, 100])
    def test_wide_fan_scaling(self, tmp_path: Path, node_count: int):
        """Wide fan with N direct deps should complete in bounded time."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _setup_wide_fan(tmp_path, node_count)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        start = time.perf_counter()
        tree = resolver.build_dependency_tree(root_yml)
        elapsed = time.perf_counter() - start

        assert len(tree.nodes) == node_count
        # Generous ceiling (5x expected) -- catches catastrophic regressions only.
        # Scaling guards in the default test suite handle O(n^2) detection.
        thresholds = {10: 10.0, 50: 25.0, 100: 50.0}
        limit = thresholds[node_count]
        assert elapsed < limit, (
            f"Wide fan ({node_count} nodes) took {elapsed:.3f}s, "
            f"expected < {limit}s (generous ceiling)"
        )


@pytest.mark.benchmark
class TestBuildDependencyTreeCorrectness:
    """Correctness checks for build_dependency_tree()."""

    def test_empty_project(self, tmp_path: Path):
        """Project with no dependencies produces an empty tree."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _write_fake_apm_yml(tmp_path, [])
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        tree = resolver.build_dependency_tree(root_yml)
        assert isinstance(tree, DependencyTree)
        assert len(tree.nodes) == 0

    def test_diamond_node_depth(self, tmp_path: Path):
        """In a diamond graph, shared dep C is at depth 2."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _setup_diamond(tmp_path)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        tree = resolver.build_dependency_tree(root_yml)

        # C should be at depth 2 (root -> A -> C or root -> B -> C)
        c_node = tree.get_node("org/c")
        assert c_node is not None
        assert c_node.depth == 2

    def test_linear_chain_depth(self, tmp_path: Path):
        """Linear chain: last node should be at depth == chain length."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        chain_len = 5
        root_yml = _setup_linear_chain(tmp_path, chain_len)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        tree = resolver.build_dependency_tree(root_yml)
        assert len(tree.nodes) == chain_len
        # Last package should be at depth == chain_len
        last_node = tree.get_node(f"org/pkg-{chain_len - 1}")
        assert last_node is not None
        assert last_node.depth == chain_len

    def test_flatten_after_build(self, tmp_path: Path):
        """flatten_dependencies on a diamond tree should not duplicate C."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        root_yml = _setup_diamond(tmp_path)
        resolver = APMDependencyResolver(
            max_depth=50,
            apm_modules_dir=tmp_path / "apm_modules",
        )

        tree = resolver.build_dependency_tree(root_yml)
        flat_map = resolver.flatten_dependencies(tree)

        assert isinstance(flat_map, FlatDependencyMap)
        # A, B, C = 3 unique deps
        assert flat_map.total_dependencies() == 3
