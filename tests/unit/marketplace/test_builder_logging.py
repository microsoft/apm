"""Logging/diagnostic tests for marketplace builder.

Verifies W1/X1/I1/I2/I3 diagnostics and quiet-path discipline per
the logging design doc.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
    ResolvedPackage,
)
from apm_cli.marketplace.errors import BuildError
from apm_cli.marketplace.migration import load_marketplace_config


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _make_config(tmp_path: Path, yml_content: str):
    _write(tmp_path / "apm.yml", yml_content)
    return load_marketplace_config(tmp_path)


def _build_local(tmp_path: Path, yml_content: str):
    """Build from a local-only config and return (doc, diagnostics)."""
    config = _make_config(tmp_path, yml_content)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entries = [e for e in config.packages if e.is_local]
    resolved = [builder._resolve_entry(e) for e in local_entries]
    doc = builder.compose_marketplace_json(resolved)
    diagnostics = getattr(builder, "_compose_diagnostics", ())
    return doc, diagnostics


# ---------------------------------------------------------------------------
# T1: Happy path -- silent (no diagnostics at warning level)
# ---------------------------------------------------------------------------


def test_pluginroot_subtraction_happy_path_silent(tmp_path: Path) -> None:
    """Clean subtraction produces no warning-level diagnostics."""
    _, diagnostics = _build_local(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          metadata:
            pluginRoot: "./plugins"
          packages:
            - name: foo
              source: ./plugins/foo
        """,
    )
    warnings = [d for d in diagnostics if d.level == "warning"]
    assert len(warnings) == 0


# ---------------------------------------------------------------------------
# T2: Verbose detail on subtraction
# ---------------------------------------------------------------------------


def test_pluginroot_subtraction_verbose_detail(tmp_path: Path) -> None:
    """Subtraction emits verbose diagnostic with before/after paths."""
    _, diagnostics = _build_local(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          metadata:
            pluginRoot: "./plugins"
          packages:
            - name: foo
              source: ./plugins/foo
        """,
    )
    verbose = [d for d in diagnostics if d.level == "verbose"]
    assert any("stripped pluginRoot" in d.message for d in verbose)
    assert any("'./plugins/foo' -> './foo'" in d.message for d in verbose)


# ---------------------------------------------------------------------------
# T3: Source outside pluginRoot warns
# ---------------------------------------------------------------------------


def test_source_outside_pluginroot_warns(tmp_path: Path) -> None:
    """Source that doesn't start with pluginRoot emits W1 warning."""
    _, diagnostics = _build_local(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          metadata:
            pluginRoot: "./plugins"
          packages:
            - name: bar
              source: ./other/bar
        """,
    )
    warnings = [d for d in diagnostics if d.level == "warning"]
    assert len(warnings) == 1
    assert "outside pluginRoot" in warnings[0].message
    assert "emitted as-is" in warnings[0].message


# ---------------------------------------------------------------------------
# T4: Empty path errors (X1)
# ---------------------------------------------------------------------------


def test_pluginroot_subtraction_empty_errors(tmp_path: Path) -> None:
    """Source == pluginRoot yields empty path -> BuildError."""
    config = _make_config(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          metadata:
            pluginRoot: "./plugins"
          packages:
            - name: bad
              source: ./plugins
        """,
    )
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    local_entries = [e for e in config.packages if e.is_local]
    resolved = [builder._resolve_entry(e) for e in local_entries]
    with pytest.raises(BuildError, match="yields empty path"):
        builder.compose_marketplace_json(resolved)


# ---------------------------------------------------------------------------
# T5/T6: Curator override -- silent default, verbose detail
# ---------------------------------------------------------------------------


def test_curator_version_override_silent_default(tmp_path: Path) -> None:
    """Override produces no warning-level diagnostic."""
    config = _make_config(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: remote-tool
              source: acme/remote-tool
              ref: v1.0.0
              version: "2.0.0"
        """,
    )
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    resolved = [
        ResolvedPackage(
            name="remote-tool",
            source_repo="acme/remote-tool",
            subdir=None,
            ref="v1.0.0",
            sha="a" * 40,
            requested_version=None,
            tags=(),
            is_prerelease=False,
        )
    ]
    builder.compose_marketplace_json(resolved)
    diagnostics = getattr(builder, "_compose_diagnostics", ())
    warnings = [d for d in diagnostics if d.level == "warning"]
    assert len(warnings) == 0


def test_curator_version_override_verbose(tmp_path: Path) -> None:
    """Override logs verbose detail when remote has a different version."""
    # This test needs a remote that returns metadata -- we use offline
    # mode which returns empty metadata, so no override diagnostic fires
    # (no remote value to override). To test the path, we mock _prefetch.
    config = _make_config(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: remote-tool
              source: acme/remote-tool
              ref: v1.0.0
              version: "2.0.0"
              description: "My desc"
        """,
    )
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    # Patch _prefetch_metadata to return remote values
    builder._prefetch_metadata = lambda resolved: {
        "remote-tool": {"description": "Remote desc", "version": "1.5.0"}
    }
    resolved = [
        ResolvedPackage(
            name="remote-tool",
            source_repo="acme/remote-tool",
            subdir=None,
            ref="v1.0.0",
            sha="a" * 40,
            requested_version=None,
            tags=(),
            is_prerelease=False,
        )
    ]
    doc = builder.compose_marketplace_json(resolved)
    diagnostics = getattr(builder, "_compose_diagnostics", ())
    verbose = [d for d in diagnostics if d.level == "verbose"]
    assert any("using curator version '2.0.0'" in d.message for d in verbose)
    assert any("using curator description" in d.message for d in verbose)
    # Verify the output uses curator values
    assert doc["plugins"][0]["version"] == "2.0.0"
    assert doc["plugins"][0]["description"] == "My desc"


# ---------------------------------------------------------------------------
# T8: Verbose summary with both clauses
# ---------------------------------------------------------------------------


def test_verbose_summary_both_clauses(tmp_path: Path) -> None:
    """Summary includes both strip count and override count."""
    config = _make_config(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          metadata:
            pluginRoot: "./plugins"
          packages:
            - name: local1
              source: ./plugins/a
            - name: local2
              source: ./plugins/b
            - name: remote-tool
              source: acme/remote
              ref: v1.0.0
              version: "2.0.0"
        """,
    )
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    builder._prefetch_metadata = lambda resolved: {"remote-tool": {"version": "1.0.0"}}
    local_entries = [e for e in config.packages if e.is_local]
    resolved = [builder._resolve_entry(e) for e in local_entries] + [
        ResolvedPackage(
            name="remote-tool",
            source_repo="acme/remote",
            subdir=None,
            ref="v1.0.0",
            sha="b" * 40,
            requested_version=None,
            tags=(),
            is_prerelease=False,
        )
    ]
    builder.compose_marketplace_json(resolved)
    diagnostics = getattr(builder, "_compose_diagnostics", ())
    summary = [d for d in diagnostics if "stripped from" in d.message]
    assert len(summary) == 1
    assert "stripped from 2 local source(s)" in summary[0].message
    assert "1 remote entry(ies) used curator-supplied overrides" in summary[0].message


# ---------------------------------------------------------------------------
# T9: Summary omitted when nothing to report
# ---------------------------------------------------------------------------


def test_verbose_summary_omitted_when_nothing(tmp_path: Path) -> None:
    """No summary when pluginRoot is unset and no overrides."""
    _, diagnostics = _build_local(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: local
              source: ./foo
        """,
    )
    summary = [
        d for d in diagnostics if "stripped from" in d.message or "curator-supplied" in d.message
    ]
    assert len(summary) == 0


# ---------------------------------------------------------------------------
# T10: New pass-through fields produce no output
# ---------------------------------------------------------------------------


def test_new_passthrough_fields_no_output(tmp_path: Path) -> None:
    """author/license/repository/keywords produce no warning."""
    _, diagnostics = _build_local(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: local
              source: ./foo
              author: "ACME Inc"
              license: "MIT"
              repository: "https://github.com/acme/tool"
              keywords: [ai, tools]
        """,
    )
    warnings = [d for d in diagnostics if d.level == "warning"]
    assert len(warnings) == 0


# ---------------------------------------------------------------------------
# T11: pluginRoot unset -- no warning for local sources
# ---------------------------------------------------------------------------


def test_pluginroot_unset_no_warning(tmp_path: Path) -> None:
    """No pluginRoot in metadata means no W1 warnings for local sources."""
    _, diagnostics = _build_local(
        tmp_path,
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: local
              source: ./foo
        """,
    )
    warnings = [d for d in diagnostics if d.level == "warning"]
    assert len(warnings) == 0
