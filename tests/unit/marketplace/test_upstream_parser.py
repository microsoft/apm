"""Tests for the strict upstream marketplace.json parser.

Counterpart to ``tests/unit/marketplace/test_models.py`` (which covers
the lenient consumer parser). The strict parser is the line of
defence for curator-side upstream resolution -- every rejection must be
named so curators see exactly which entry failed and why.
"""

from __future__ import annotations

import pytest

from apm_cli.marketplace.upstream_parser import (
    REJECTION_REASONS,
    StrictManifest,
    StrictPlugin,
    StrictPluginSource,
    StrictRejection,
    parse_marketplace_strict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(
    plugins: list[dict],
    *,
    metadata: dict | None = None,
    upstream_owner_repo: str = "abhigyanpatwari/GitNexus",
    upstream_host: str = "github.com",
) -> StrictManifest:
    data: dict = {
        "name": "gitnexus-marketplace",
        "owner": {"name": "GitNexus", "email": "x@example.com"},
        "plugins": plugins,
    }
    if metadata is not None:
        data["metadata"] = metadata
    return parse_marketplace_strict(
        data,
        upstream_owner_repo=upstream_owner_repo,
        upstream_host=upstream_host,
    )


def _only_rejection(manifest: StrictManifest) -> StrictRejection:
    """Assert exactly one rejection and return it."""
    assert len(manifest.plugins) == 0, (
        f"expected no accepted plugins, got {[p.name for p in manifest.plugins]}"
    )
    assert len(manifest.rejections) == 1, (
        f"expected exactly one rejection, got {len(manifest.rejections)}"
    )
    return manifest.rejections[0]


# ---------------------------------------------------------------------------
# Manifest-level shape
# ---------------------------------------------------------------------------


class TestManifestRoot:
    def test_root_must_be_object(self):
        with pytest.raises(TypeError, match="root must be a JSON object"):
            parse_marketplace_strict(
                ["not", "an", "object"],  # type: ignore[arg-type]
                upstream_owner_repo="o/r",
            )

    def test_unsupported_host_parsed_without_rejection(self):
        # B4: host validation is performed by the resolver layer, NOT the
        # parser. The parser must accept non-github.com upstream hosts so
        # that GHE upstreams work without a parse-time gate.
        manifest = parse_marketplace_strict(
            {"name": "x", "plugins": []},
            upstream_owner_repo="o/r",
            upstream_host="gitlab.com",
        )
        assert manifest.rejections == ()
        assert manifest.plugins == ()

    def test_invalid_owner_repo_raises(self):
        # Wrong-shape upstream coordinates are a programming error
        # (the schema already validated them), so we raise rather
        # than emitting a per-entry rejection.
        with pytest.raises(ValueError, match="owner/repo"):
            parse_marketplace_strict(
                {"name": "x", "plugins": []},
                upstream_owner_repo="bad-shape",
            )

    def test_plugins_not_list_yields_rejection(self):
        manifest = parse_marketplace_strict(
            {"name": "x", "plugins": "not-a-list"},
            upstream_owner_repo="o/r",
        )
        assert manifest.plugins == ()
        assert len(manifest.rejections) == 1
        assert manifest.rejections[0].reason == "invalid-source-shape"

    def test_owner_dict_extracted(self):
        manifest = _parse([])
        assert manifest.owner_name == "GitNexus"

    def test_owner_string_tolerated(self):
        data = {"name": "x", "owner": "ACME", "plugins": []}
        manifest = parse_marketplace_strict(data, upstream_owner_repo="o/r")
        assert manifest.owner_name == "ACME"

    def test_plugin_root_normalised(self):
        manifest = _parse([], metadata={"pluginRoot": "./plugins"})
        assert manifest.plugin_root == "plugins"

    def test_known_rejection_reasons_form_closed_set(self):
        # The published REJECTION_REASONS set must stay closed -- if a
        # contributor adds a new reason without listing it here, the
        # CLI rejection-reason mapping will silently miss it.
        assert "missing-source" in REJECTION_REASONS
        assert "invalid-repo" in REJECTION_REASONS
        assert "npm-source" in REJECTION_REASONS


# ---------------------------------------------------------------------------
# Source-shape support matrix -- happy paths
# ---------------------------------------------------------------------------


class TestSourceShapesHappy:
    def test_repository_shape_minimal(self):
        manifest = _parse(
            [
                {
                    "name": "p",
                    "repository": "owner/repo",
                }
            ]
        )
        assert manifest.rejections == ()
        assert len(manifest.plugins) == 1
        plugin = manifest.plugins[0]
        assert isinstance(plugin, StrictPlugin)
        assert plugin.source == StrictPluginSource(
            type="github",
            host="github.com",
            repo="owner/repo",
        )

    def test_repository_shape_with_ref_and_sha(self):
        sha = "a" * 40
        manifest = _parse(
            [
                {
                    "name": "p",
                    "repository": "owner/repo",
                    "ref": "v1.2.3",
                    "sha": sha,
                }
            ]
        )
        assert manifest.rejections == ()
        plugin = manifest.plugins[0]
        assert plugin.source.ref == "v1.2.3"
        assert plugin.source.sha == sha

    def test_github_dict_shape(self):
        manifest = _parse(
            [
                {
                    "name": "p",
                    "source": {
                        "type": "github",
                        "repo": "owner/repo",
                        "ref": "main",
                    },
                }
            ]
        )
        plugin = manifest.plugins[0]
        assert plugin.source.type == "github"
        assert plugin.source.repo == "owner/repo"
        assert plugin.source.ref == "main"
        assert plugin.source.subdir is None

    def test_git_subdir_dict_shape(self):
        manifest = _parse(
            [
                {
                    "name": "p",
                    "source": {
                        "type": "git-subdir",
                        "repo": "owner/repo",
                        "path": "subpath/here",
                        "ref": "v1.0.0",
                    },
                }
            ]
        )
        plugin = manifest.plugins[0]
        assert plugin.source.type == "git-subdir"
        assert plugin.source.subdir == "subpath/here"

    def test_string_shorthand_with_plugin_root(self):
        # GitNexus uses this shape exactly.
        manifest = _parse(
            [
                {
                    "name": "gitnexus",
                    "source": "./gitnexus-claude-plugin",
                }
            ],
            metadata={"pluginRoot": "."},
        )
        plugin = manifest.plugins[0]
        assert plugin.source.type == "git-subdir"
        assert plugin.source.repo == "abhigyanpatwari/GitNexus"
        assert plugin.source.subdir == "gitnexus-claude-plugin"

    def test_string_shorthand_with_explicit_plugin_root(self):
        manifest = _parse(
            [{"name": "p", "source": "./foo"}],
            metadata={"pluginRoot": "plugins"},
        )
        plugin = manifest.plugins[0]
        assert plugin.source.subdir == "plugins/foo"

    def test_bare_name_string_source(self):
        # ``foo`` (no ``./``) is also acceptable.
        manifest = _parse([{"name": "p", "source": "foo"}])
        plugin = manifest.plugins[0]
        assert plugin.source.type == "git-subdir"
        assert plugin.source.subdir == "foo"


# ---------------------------------------------------------------------------
# Per-entry rejections (named-reason contract)
# ---------------------------------------------------------------------------


class TestEntryRejections:
    def test_missing_name_rejected(self):
        rej = _only_rejection(_parse([{"repository": "o/r"}]))
        assert rej.reason == "missing-name"
        assert rej.plugin_name == "<unnamed>"

    def test_blank_name_rejected(self):
        rej = _only_rejection(_parse([{"name": "  ", "repository": "o/r"}]))
        assert rej.reason == "missing-name"

    def test_unknown_plugin_key_rejected(self):
        rej = _only_rejection(_parse([{"name": "p", "repository": "o/r", "soruce": "typo"}]))
        assert rej.reason == "unknown-plugin-key"
        assert "soruce" in rej.detail

    def test_missing_source_and_repository(self):
        rej = _only_rejection(_parse([{"name": "p"}]))
        assert rej.reason == "missing-source"

    def test_both_source_and_repository_ambiguous(self):
        rej = _only_rejection(
            _parse(
                [
                    {
                        "name": "p",
                        "repository": "o/r",
                        "source": {"type": "github", "repo": "o/r"},
                    }
                ]
            )
        )
        assert rej.reason == "ambiguous-source"

    def test_invalid_repository_string(self):
        rej = _only_rejection(_parse([{"name": "p", "repository": "no-slash"}]))
        assert rej.reason == "invalid-repo"

    def test_npm_source_type_rejected(self):
        rej = _only_rejection(
            _parse(
                [
                    {
                        "name": "p",
                        "source": {"type": "npm", "package": "@foo/bar"},
                    }
                ]
            )
        )
        assert rej.reason == "npm-source"

    def test_unsupported_source_type(self):
        rej = _only_rejection(_parse([{"name": "p", "source": {"type": "tarball", "url": "x"}}]))
        assert rej.reason == "unsupported-source-type"

    def test_unknown_source_key(self):
        rej = _only_rejection(
            _parse(
                [
                    {
                        "name": "p",
                        "source": {
                            "type": "github",
                            "repo": "o/r",
                            "extraneous": "junk",
                        },
                    }
                ]
            )
        )
        assert rej.reason == "unknown-source-key"

    def test_cross_host_source_accepted_by_parser(self):
        # B4: host validation is performed by the resolver layer. The parser
        # accepts cross-host source objects (e.g. a plugin hosted on an
        # enterprise GHE instance) and stores the host so the resolver can
        # apply its own per-host auth logic.
        manifest = _parse(
            [
                {
                    "name": "p",
                    "source": {
                        "type": "github",
                        "repo": "o/r",
                        "host": "gitlab.com",
                    },
                }
            ]
        )
        assert manifest.rejections == ()
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].source.host == "gitlab.com"

    def test_url_form_rejected(self):
        rej = _only_rejection(
            _parse(
                [
                    {
                        "name": "p",
                        "source": {
                            "type": "git-subdir",
                            "url": "https://github.com/o/r",
                            "path": "p",
                        },
                    }
                ]
            )
        )
        assert rej.reason == "invalid-repo"

    def test_invalid_repo_in_dict_source(self):
        rej = _only_rejection(
            _parse([{"name": "p", "source": {"type": "github", "repo": "no-slash"}}])
        )
        assert rej.reason == "invalid-repo"

    def test_git_subdir_missing_path(self):
        rej = _only_rejection(
            _parse([{"name": "p", "source": {"type": "git-subdir", "repo": "o/r"}}])
        )
        assert rej.reason == "missing-subdir"

    def test_path_traversal_in_dict_source(self):
        rej = _only_rejection(
            _parse(
                [
                    {
                        "name": "p",
                        "source": {
                            "type": "git-subdir",
                            "repo": "o/r",
                            "path": "../escape",
                        },
                    }
                ]
            )
        )
        assert rej.reason == "path-traversal"

    def test_path_traversal_in_string_source(self):
        rej = _only_rejection(_parse([{"name": "p", "source": "../escape"}]))
        assert rej.reason == "path-traversal"

    def test_absolute_path_in_string_source(self):
        rej = _only_rejection(_parse([{"name": "p", "source": "/etc/passwd"}]))
        assert rej.reason == "path-traversal"

    def test_url_encoded_traversal_in_string_source(self):
        rej = _only_rejection(_parse([{"name": "p", "source": "%2e%2e/secret"}]))
        assert rej.reason == "path-traversal"

    def test_invalid_ref_shape(self):
        rej = _only_rejection(_parse([{"name": "p", "repository": "o/r", "ref": "has space"}]))
        assert rej.reason == "invalid-ref"

    def test_invalid_sha_truncated(self):
        rej = _only_rejection(_parse([{"name": "p", "repository": "o/r", "sha": "abcd1234"}]))
        assert rej.reason == "invalid-sha"

    def test_invalid_source_shape_non_string_non_dict(self):
        rej = _only_rejection(_parse([{"name": "p", "source": 42}]))
        assert rej.reason == "invalid-source-shape"

    def test_empty_string_source(self):
        # Empty source string is structurally present but unusable;
        # the parser surfaces this as ``invalid-source-shape`` rather
        # than ``missing-source`` so the curator sees a different
        # error than for a truly absent ``source`` key.
        rej = _only_rejection(_parse([{"name": "p", "source": ""}]))
        assert rej.reason == "invalid-source-shape"

    def test_dict_source_missing_type_with_repo_infers_github(self):
        # B3: the short-form ``{"repo": "owner/repo", "ref": "..."}`` that
        # some APM and Anthropic marketplaces emit without an explicit ``type``
        # key is now accepted by inferring ``type=github`` when ``repo`` is
        # present. Hard-reject only when ``type`` is present but unrecognised.
        manifest = _parse([{"name": "p", "source": {"repo": "o/r"}}])
        assert manifest.rejections == ()
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].source.type == "github"

    def test_dict_source_missing_type_and_repo_yields_rejection(self):
        # Still a rejection when both ``type`` and ``repo`` are absent --
        # the parser cannot infer a source shape.
        rej = _only_rejection(_parse([{"name": "p", "source": {}}]))
        assert rej.reason == "invalid-source-shape"

    def test_non_dict_plugin_entry(self):
        manifest = _parse(["not-a-dict"])  # type: ignore[list-item]
        rej = _only_rejection(manifest)
        assert rej.reason == "invalid-source-shape"


# ---------------------------------------------------------------------------
# Multi-entry behaviour
# ---------------------------------------------------------------------------


class TestMultiEntry:
    def test_partial_success_does_not_drop_good_entries(self):
        manifest = _parse(
            [
                {"name": "good", "repository": "o/r"},
                {"name": "bad", "source": {"type": "npm"}},
                {"name": "also-good", "repository": "o2/r2"},
            ]
        )
        names = [p.name for p in manifest.plugins]
        assert names == ["good", "also-good"]
        assert len(manifest.rejections) == 1
        assert manifest.rejections[0].plugin_name == "bad"

    def test_duplicate_name_rejected(self):
        manifest = _parse(
            [
                {"name": "p", "repository": "o/r"},
                {"name": "p", "repository": "o/r2"},
            ]
        )
        # First wins, second rejected.
        assert len(manifest.plugins) == 1
        assert len(manifest.rejections) == 1
        assert manifest.rejections[0].reason == "duplicate-name"

    def test_find_plugin_is_case_sensitive(self):
        manifest = _parse([{"name": "GitNexus", "repository": "o/r"}])
        assert manifest.find_plugin("GitNexus") is not None
        # Strict lookup: lowercase mismatch is a miss.
        assert manifest.find_plugin("gitnexus") is None


# ---------------------------------------------------------------------------
# Real-world fixture: GitNexus marketplace.json shape
# ---------------------------------------------------------------------------


class TestGitNexusFixture:
    """Locks the contract against the real upstream marketplace shape.

    Mirrors ``https://raw.githubusercontent.com/abhigyanpatwari/GitNexus/main/.claude-plugin/marketplace.json``
    -- if a future schema change to GitNexus breaks this test, the
    upstream-parser support matrix needs to be revisited.
    """

    def test_gitnexus_top_level_fixture_parses_cleanly(self):
        data = {
            "name": "gitnexus-marketplace",
            "owner": {"name": "GitNexus", "email": "nico@gitnexus.dev"},
            "metadata": {
                "description": "Code intelligence powered by a knowledge graph",
                "homepage": "https://github.com/nicosxt/gitnexus",
            },
            "plugins": [
                {
                    "name": "gitnexus",
                    "version": "1.3.3",
                    "source": "./gitnexus-claude-plugin",
                    "description": "Code intelligence",
                }
            ],
        }
        manifest = parse_marketplace_strict(data, upstream_owner_repo="abhigyanpatwari/GitNexus")
        assert manifest.rejections == ()
        assert manifest.name == "gitnexus-marketplace"
        assert manifest.owner_name == "GitNexus"
        assert len(manifest.plugins) == 1
        plugin = manifest.plugins[0]
        assert plugin.name == "gitnexus"
        assert plugin.version == "1.3.3"
        assert plugin.source.type == "git-subdir"
        assert plugin.source.repo == "abhigyanpatwari/GitNexus"
        assert plugin.source.subdir == "gitnexus-claude-plugin"
