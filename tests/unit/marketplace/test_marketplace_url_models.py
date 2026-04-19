"""Tests for URL-based marketplace source and Agent Skills index parser.

Covers MarketplaceSource URL fields, serialization round-trips, and the
parse_agent_skills_index() parser including schema enforcement, skill name
validation, and source type handling.
"""

import pytest

from apm_cli.marketplace.models import (
    MarketplaceSource,
    parse_agent_skills_index,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_VALID_DIGEST = "sha256:" + "a" * 64
_KNOWN_SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"

_SINGLE_SKILL_INDEX = {
    "$schema": _KNOWN_SCHEMA,
    "skills": [
        {
            "name": "code-review",
            "type": "skill-md",
            "description": "Code review helper",
            "url": "/.well-known/agent-skills/code-review/SKILL.md",
            "digest": _VALID_DIGEST,
        }
    ],
}


# ---------------------------------------------------------------------------
# MarketplaceSource -- URL fields
# ---------------------------------------------------------------------------


class TestMarketplaceSourceURL:
    """MarketplaceSource extended with source_type='url'."""

    def test_url_source_creation(self):
        src = MarketplaceSource(
            name="example-skills",
            source_type="url",
            url="https://example.com/.well-known/agent-skills/index.json",
        )
        assert src.source_type == "url"
        assert src.url == "https://example.com/.well-known/agent-skills/index.json"
        assert src.owner == ""
        assert src.repo == ""

    def test_github_source_type_default(self):
        """Existing GitHub sources default to source_type='github'."""
        src = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        assert src.source_type == "github"

    def test_url_source_frozen(self):
        src = MarketplaceSource(
            name="x", source_type="url", url="https://example.com"
        )
        with pytest.raises(AttributeError):
            src.url = "https://other.com"

    def test_is_url_source_true(self):
        src = MarketplaceSource(
            name="x", source_type="url", url="https://example.com"
        )
        assert src.is_url_source is True

    def test_is_url_source_false_for_github(self):
        src = MarketplaceSource(name="x", owner="o", repo="r")
        assert src.is_url_source is False

    # --- to_dict ---

    def test_url_source_to_dict_contains_source_type_and_url(self):
        src = MarketplaceSource(
            name="example-skills",
            source_type="url",
            url="https://example.com/.well-known/agent-skills/index.json",
        )
        d = src.to_dict()
        assert d["source_type"] == "url"
        assert d["url"] == "https://example.com/.well-known/agent-skills/index.json"

    def test_url_source_to_dict_omits_owner_and_repo(self):
        src = MarketplaceSource(
            name="example-skills",
            source_type="url",
            url="https://example.com/.well-known/agent-skills/index.json",
        )
        d = src.to_dict()
        assert "owner" not in d
        assert "repo" not in d

    def test_github_source_to_dict_omits_source_type(self):
        """GitHub sources must not add source_type to preserve backward compat."""
        src = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        d = src.to_dict()
        assert "source_type" not in d

    # --- from_dict ---

    def test_url_source_from_dict(self):
        d = {
            "name": "example-skills",
            "source_type": "url",
            "url": "https://example.com/.well-known/agent-skills/index.json",
        }
        src = MarketplaceSource.from_dict(d)
        assert src.source_type == "url"
        assert src.url == "https://example.com/.well-known/agent-skills/index.json"
        assert src.owner == ""
        assert src.repo == ""

    def test_github_from_dict_backward_compat_no_source_type(self):
        """Old dicts without source_type field deserialize as 'github'."""
        d = {"name": "acme", "owner": "acme-org", "repo": "plugins"}
        src = MarketplaceSource.from_dict(d)
        assert src.source_type == "github"

    def test_github_from_dict_explicit_source_type(self):
        """Explicit source_type='github' in dict is honoured by from_dict."""
        d = {"name": "acme", "owner": "acme-org", "repo": "plugins", "source_type": "github"}
        src = MarketplaceSource.from_dict(d)
        assert src.source_type == "github"
        assert src.owner == "acme-org"

    def test_unknown_source_type_raises(self):
        """from_dict with unrecognised source_type must raise ValueError."""
        d = {"name": "x", "source_type": "artifactory", "url": "https://art.corp.com/index.json"}
        with pytest.raises(ValueError, match="artifactory"):
            MarketplaceSource.from_dict(d)

    def test_url_source_missing_url_raises(self):
        """from_dict with source_type='url' but no url key must raise."""
        d = {"name": "x", "source_type": "url"}
        with pytest.raises(ValueError, match="non-empty"):
            MarketplaceSource.from_dict(d)

    def test_url_source_empty_url_raises(self):
        """from_dict with source_type='url' and empty url must raise."""
        d = {"name": "x", "source_type": "url", "url": ""}
        with pytest.raises(ValueError, match="non-empty"):
            MarketplaceSource.from_dict(d)

    def test_url_source_to_dict_omits_github_only_fields(self):
        """URL to_dict must not include host, branch, or path."""
        src = MarketplaceSource(
            name="x", source_type="url", url="https://example.com"
        )
        d = src.to_dict()
        assert "host" not in d
        assert "branch" not in d
        assert "path" not in d

    # --- roundtrip ---

    def test_url_source_roundtrip(self):
        original = MarketplaceSource(
            name="example-skills",
            source_type="url",
            url="https://example.com/.well-known/agent-skills/index.json",
        )
        restored = MarketplaceSource.from_dict(original.to_dict())
        assert restored == original

    def test_github_source_roundtrip_unchanged(self):
        """Existing GitHub roundtrip still works after model changes."""
        original = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            host="ghe.corp.com",
            branch="release",
        )
        restored = MarketplaceSource.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# parse_agent_skills_index
# ---------------------------------------------------------------------------


class TestParseAgentSkillsIndex:
    """Parser for Agent Skills Discovery RFC v0.2.0 index format."""

    # --- happy path ---

    def test_basic_parse_returns_manifest(self):
        manifest = parse_agent_skills_index(_SINGLE_SKILL_INDEX, "example-skills")
        assert manifest.name == "example-skills"
        assert len(manifest.plugins) == 1

    def test_skill_entry_fields(self):
        manifest = parse_agent_skills_index(_SINGLE_SKILL_INDEX, "test")
        p = manifest.plugins[0]
        assert p.name == "code-review"
        assert p.description == "Code review helper"
        assert p.source_marketplace == "test"

    def test_skill_source_contains_url_digest_and_type(self):
        manifest = parse_agent_skills_index(_SINGLE_SKILL_INDEX, "test")
        s = manifest.plugins[0].source
        assert isinstance(s, dict)
        assert s["url"] == "/.well-known/agent-skills/code-review/SKILL.md"
        assert s["digest"] == _VALID_DIGEST
        assert s["type"] == "skill-md"

    def test_archive_type_entry(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {
                    "name": "my-toolset",
                    "type": "archive",
                    "description": "A set of tools",
                    "url": "/.well-known/agent-skills/my-toolset.tar.gz",
                    "digest": _VALID_DIGEST,
                }
            ],
        }
        manifest = parse_agent_skills_index(data, "test")
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].source["type"] == "archive"

    def test_multiple_skills(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {
                    "name": "skill-one",
                    "type": "skill-md",
                    "description": "First",
                    "url": "/a/SKILL.md",
                    "digest": _VALID_DIGEST,
                },
                {
                    "name": "skill-two",
                    "type": "archive",
                    "description": "Second",
                    "url": "/b.tar.gz",
                    "digest": _VALID_DIGEST,
                },
            ],
        }
        manifest = parse_agent_skills_index(data, "multi")
        assert len(manifest.plugins) == 2
        assert manifest.find_plugin("skill-one") is not None
        assert manifest.find_plugin("skill-two") is not None

    def test_empty_skills_list(self):
        data = {"$schema": _KNOWN_SCHEMA, "skills": []}
        manifest = parse_agent_skills_index(data, "test")
        assert len(manifest.plugins) == 0

    def test_missing_skills_key_returns_empty_manifest(self):
        """No 'skills' key present -- returns an empty manifest rather than raising."""
        data = {"$schema": _KNOWN_SCHEMA}
        manifest = parse_agent_skills_index(data, "test")
        assert len(manifest.plugins) == 0

    # --- $schema enforcement ---

    def test_known_schema_accepted(self):
        manifest = parse_agent_skills_index(_SINGLE_SKILL_INDEX, "test")
        assert len(manifest.plugins) == 1

    def test_unknown_schema_version_raises(self):
        data = {
            "$schema": "https://schemas.agentskills.io/discovery/9.9.9/schema.json",
            "skills": [],
        }
        with pytest.raises(ValueError, match="schema"):
            parse_agent_skills_index(data, "test")

    def test_missing_schema_raises(self):
        with pytest.raises(ValueError, match="schema"):
            parse_agent_skills_index({"skills": []}, "test")

    def test_non_string_schema_raises(self):
        with pytest.raises(ValueError, match="schema"):
            parse_agent_skills_index({"$schema": 42, "skills": []}, "test")

    # --- skill name validation (RFC: 1-64 chars, lowercase alnum + hyphens) ---

    @pytest.mark.parametrize("name", [
        "my-skill",
        "skill-v2-final",
        "a",
        "a" * 64,
        "123",
    ])
    def test_valid_name_accepted(self, name):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": name, "type": "skill-md", "url": "/x", "digest": _VALID_DIGEST}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 1

    @pytest.mark.parametrize("name", [
        "MySkill",
        "bad name",
        "-bad",
        "bad-",
        "bad--name",
        "a" * 65,
        "my_skill",
        "my.skill",
        "",
    ])
    def test_invalid_name_skipped(self, name):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": name, "type": "skill-md", "url": "/x", "digest": _VALID_DIGEST}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    def test_missing_name_skipped(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"type": "skill-md", "url": "/x", "digest": _VALID_DIGEST}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    # --- mixed valid/invalid entries ---

    def test_only_valid_entries_returned(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "good-skill", "type": "skill-md", "url": "/a", "digest": _VALID_DIGEST},
                {"name": "Bad Skill!", "type": "skill-md", "url": "/b", "digest": _VALID_DIGEST},
                {"type": "skill-md", "url": "/c", "digest": _VALID_DIGEST},  # no name
            ],
        }
        manifest = parse_agent_skills_index(data, "test")
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].name == "good-skill"

    # --- non-string / non-dict entry handling ---

    def test_non_string_name_skipped(self):
        """Integer name field must not raise AttributeError (bug guard)."""
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": 42, "type": "skill-md", "url": "/x", "digest": _VALID_DIGEST}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    def test_non_dict_entry_in_skills_skipped(self):
        """Non-dict items in skills list are silently skipped."""
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                "not-a-dict",
                {"name": "good-skill", "type": "skill-md", "url": "/a", "digest": _VALID_DIGEST},
            ],
        }
        manifest = parse_agent_skills_index(data, "t")
        assert len(manifest.plugins) == 1

    def test_skills_not_a_list_returns_empty(self):
        """If 'skills' is not a list, parser warns and returns empty manifest."""
        data = {"$schema": _KNOWN_SCHEMA, "skills": {"name": "oops"}}
        manifest = parse_agent_skills_index(data, "t")
        assert len(manifest.plugins) == 0

    # --- source_name / manifest.name ---

    def test_empty_source_name_yields_unknown_manifest_name(self):
        """Empty source_name falls back to 'unknown' in manifest.name."""
        manifest = parse_agent_skills_index(_SINGLE_SKILL_INDEX, "")
        assert manifest.name == "unknown"

    # --- optional entry fields default to empty string ---

    def test_missing_description_defaults_to_empty(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "url": "/x", "digest": _VALID_DIGEST}
            ],
        }
        p = parse_agent_skills_index(data, "t").plugins[0]
        assert p.description == ""

    def test_missing_url_in_entry_is_skipped(self):
        """Entries without a url field are now skipped (C07 fix)."""
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "digest": _VALID_DIGEST}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    def test_entry_without_digest_is_skipped(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "url": "/x"}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    # --- duplicate names ---

    def test_duplicate_skill_names_both_accepted(self):
        """Parser does not deduplicate; both entries are returned."""
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "url": "/a", "digest": _VALID_DIGEST},
                {"name": "my-skill", "type": "archive", "url": "/b.tar.gz", "digest": _VALID_DIGEST},
            ],
        }
        manifest = parse_agent_skills_index(data, "t")
        assert len(manifest.plugins) == 2


# ---------------------------------------------------------------------------
# MarketplaceManifest -- source provenance fields (t5-test-04)
# ---------------------------------------------------------------------------


class TestManifestSourceFields:
    """MarketplaceManifest must carry source_url and source_digest for provenance."""

    def test_parse_agent_skills_index_default_source_fields_are_empty(self):
        manifest = parse_agent_skills_index(_SINGLE_SKILL_INDEX, "test")
        assert manifest.source_url == ""
        assert manifest.source_digest == ""

    def test_parse_agent_skills_index_accepts_source_url_kwarg(self):
        manifest = parse_agent_skills_index(
            _SINGLE_SKILL_INDEX, "test",
            source_url="https://example.com/.well-known/agent-skills/index.json",
        )
        assert manifest.source_url == "https://example.com/.well-known/agent-skills/index.json"

    def test_parse_agent_skills_index_accepts_source_digest_kwarg(self):
        manifest = parse_agent_skills_index(
            _SINGLE_SKILL_INDEX, "test",
            source_digest="sha256:" + "a" * 64,
        )
        assert manifest.source_digest == "sha256:" + "a" * 64


# ---------------------------------------------------------------------------
# Digest format validation (t5-test-06)
# ---------------------------------------------------------------------------


class TestDigestFormatValidation:
    """parse_agent_skills_index must skip entries with malformed digest values."""

    def test_valid_digest_entry_is_included(self):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "ok-skill", "type": "skill-md", "url": "/x", "digest": _VALID_DIGEST}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 1

    @pytest.mark.parametrize("digest", [
        "md5:abc123",
        "sha256:abc",
        "SHA256:" + "a" * 64,
    ])
    def test_malformed_digest_entry_skipped(self, digest):
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "bad-skill", "type": "skill-md", "url": "/x", "digest": digest}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    def test_missing_digest_entry_skipped(self):
        """A skill entry with no digest is skipped -- digest is required by the RFC."""
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "no-digest", "type": "skill-md", "url": "/x"}
            ],
        }
        assert len(parse_agent_skills_index(data, "t").plugins) == 0

    def test_valid_and_invalid_digest_only_valid_included(self):
        """Mixed entries: only those with a valid digest are returned."""
        data = {
            "$schema": _KNOWN_SCHEMA,
            "skills": [
                {"name": "good", "type": "skill-md", "url": "/a", "digest": _VALID_DIGEST},
                {"name": "bad", "type": "skill-md", "url": "/b", "digest": "sha256:short"},
            ],
        }
        plugins = parse_agent_skills_index(data, "t").plugins
        assert len(plugins) == 1
        assert plugins[0].name == "good"


# ---------------------------------------------------------------------------
# E2 / T11: warning level for invalid skill name entries
# ---------------------------------------------------------------------------


class TestInvalidNameLogLevel:
    """parse_agent_skills_index must emit WARNING (not DEBUG) for invalid names."""

    def test_invalid_names_emit_warning_not_debug(self, caplog):
        import logging

        _V = "sha256:" + "a" * 64
        data = {
            "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
            "skills": [
                {"name": "INVALID_UPPER", "type": "skill-md", "url": "/a", "digest": _V},
                {"name": "also-invalid!", "type": "skill-md", "url": "/b", "digest": _V},
                {"name": "valid-skill", "type": "skill-md", "url": "/c", "digest": _V},
            ],
        }
        with caplog.at_level(logging.WARNING, logger="apm_cli.marketplace.models"):
            result = parse_agent_skills_index(data, "test-src")

        assert len(result.plugins) == 1
        assert result.plugins[0].name == "valid-skill"
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_messages) == 2

    def test_structural_issues_do_not_produce_warnings(self, caplog):
        """Non-dict entries and missing name are structural noise -- keep at DEBUG."""
        import logging

        data = {
            "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
            "skills": [
                "not-a-dict",
                None,
            ],
        }
        with caplog.at_level(logging.WARNING, logger="apm_cli.marketplace.models"):
            result = parse_agent_skills_index(data, "test-src")

        assert len(result.plugins) == 0
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_messages) == 0


class TestSkillFieldValidation:
    """parse_agent_skills_index must validate type/url/description fields."""

    _SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
    _DIGEST = "sha256:" + "a" * 64

    def test_unsupported_type_skipped(self):
        data = {
            "$schema": self._SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "unknown-type", "url": "/a", "digest": self._DIGEST},
            ],
        }
        result = parse_agent_skills_index(data, "s")
        assert len(result.plugins) == 0

    def test_non_string_type_skipped(self):
        data = {
            "$schema": self._SCHEMA,
            "skills": [
                {"name": "my-skill", "type": 123, "url": "/a", "digest": self._DIGEST},
            ],
        }
        result = parse_agent_skills_index(data, "s")
        assert len(result.plugins) == 0

    def test_missing_url_skipped(self):
        data = {
            "$schema": self._SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "digest": self._DIGEST},
            ],
        }
        result = parse_agent_skills_index(data, "s")
        assert len(result.plugins) == 0

    def test_non_string_url_skipped(self):
        data = {
            "$schema": self._SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "url": 42, "digest": self._DIGEST},
            ],
        }
        result = parse_agent_skills_index(data, "s")
        assert len(result.plugins) == 0

    def test_non_string_description_defaults_to_empty(self):
        data = {
            "$schema": self._SCHEMA,
            "skills": [
                {"name": "my-skill", "type": "skill-md", "url": "/a",
                 "digest": self._DIGEST, "description": 42},
            ],
        }
        result = parse_agent_skills_index(data, "s")
        assert len(result.plugins) == 1
        assert result.plugins[0].description == ""

    def test_valid_skill_md_and_archive_accepted(self):
        data = {
            "$schema": self._SCHEMA,
            "skills": [
                {"name": "skill-a", "type": "skill-md", "url": "/a", "digest": self._DIGEST},
                {"name": "skill-b", "type": "archive", "url": "/b", "digest": self._DIGEST},
            ],
        }
        result = parse_agent_skills_index(data, "s")
        assert len(result.plugins) == 2
