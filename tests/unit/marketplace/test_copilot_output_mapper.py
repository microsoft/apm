"""Copilot marketplace source mapping follows the documented consumer contract."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlparse

import pytest

from apm_cli.marketplace.builder import ResolvedPackage
from apm_cli.marketplace.errors import BuildError
from apm_cli.marketplace.output_mappers import CopilotMarketplaceMapper
from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS
from apm_cli.marketplace.yml_schema import (
    MarketplaceConfig,
    MarketplaceOwner,
    MarketplaceYmlError,
    PackageEntry,
    _parse_copilot,
)

pytestmark = pytest.mark.unit
SHA = "0123456789abcdef0123456789abcdef01234567"


def _resolved(**overrides: object) -> ResolvedPackage:
    return replace(
        ResolvedPackage("demo", "acme/demo", None, "v2.1.0", SHA, None, (), False),
        **overrides,
    )


def _compose(
    entry: PackageEntry, pkg: ResolvedPackage, remote_metadata: dict | None = None
) -> dict:
    config = MarketplaceConfig(
        name="my-marketplace",
        description="Curated plugins",
        version="1.0.0",
        owner=MarketplaceOwner(name="Acme", email="plugins@acme.test"),
        outputs=("copilot",),
        packages=(entry,),
    )
    return (
        CopilotMarketplaceMapper()
        .compose(config=config, resolved=(pkg,), remote_metadata=remote_metadata)
        .document
    )


def test_copilot_profile_uses_default_discovery_path() -> None:
    profile = MARKETPLACE_OUTPUTS["copilot"]
    assert profile.default_output == ".github/plugin/marketplace.json"
    assert profile.mapper == "copilot"
    assert profile.path_env_var == "APM_MARKETPLACE_COPILOT_PATH"


def test_copilot_mapper_nests_marketplace_metadata_and_keeps_local_source() -> None:
    doc = _compose(
        PackageEntry(
            name="demo",
            source="./plugins/demo",
            version="2.1.0",
            description="Demo plugin",
            is_local=True,
        ),
        _resolved(source_repo="", ref="", sha=""),
    )
    assert doc["metadata"] == {"description": "Curated plugins", "version": "1.0.0"}
    assert "version" not in doc and "description" not in doc
    assert doc["plugins"] == [
        {
            "name": "demo",
            "description": "Demo plugin",
            "version": "2.1.0",
            "source": "./plugins/demo",
        }
    ]


@pytest.mark.parametrize("subdir", [None, "plugins/demo", "./plugins/demo", "plugins/demo/"])
def test_copilot_remote_github_preserves_identity_and_pins(subdir: str | None) -> None:
    source = _compose(
        PackageEntry(name="demo", source="acme/demo", ref="v2.1.0"),
        _resolved(subdir=subdir),
    )["plugins"][0]["source"]
    assert source == {
        "source": "github",
        "repo": "acme/demo",
        "ref": "v2.1.0",
        "sha": SHA,
        **({"path": subdir} if subdir else {}),
    }


@pytest.mark.parametrize(
    ("host", "url", "scheme", "expected_host", "path"),
    [
        ("ghe.example.com", None, "https", "ghe.example.com", "/acme/demo"),
        (
            None,
            "https://gitlab.example.com/team/demo.git",
            "https",
            "gitlab.example.com",
            "/team/demo.git",
        ),
        (
            None,
            "ssh://git@git.example.com:2222/team/demo.git",
            "ssh",
            "git.example.com",
            "/team/demo.git",
        ),
    ],
)
def test_copilot_remote_url_preserves_transport(
    host: str | None, url: str | None, scheme: str, expected_host: str, path: str
) -> None:
    source = _compose(
        PackageEntry(name="demo", source="acme/demo", ref="v2.1.0"),
        _resolved(host=host, source_url=url, subdir="plugins/demo"),
    )["plugins"][0]["source"]
    assert source["source"] == "url"
    parsed = urlparse(source["url"])
    assert (parsed.scheme, parsed.hostname, parsed.path) == (scheme, expected_host, path)
    if scheme == "ssh":
        assert (parsed.username, parsed.port) == ("git", 2222)
    assert source["ref"] == "v2.1.0"
    assert source["sha"] == SHA
    assert source["path"] == "plugins/demo"


@pytest.mark.parametrize("ref", [SHA, SHA.upper(), ""])
def test_copilot_commit_pin_is_not_a_cloneable_ref(ref: str) -> None:
    source = _compose(
        PackageEntry(name="demo", source="acme/demo", ref=SHA),
        _resolved(ref=ref),
    )["plugins"][0]["source"]
    assert "ref" not in source
    assert source["sha"].lower() == SHA
    assert source["repo"] == "acme/demo"


def test_copilot_uses_peeled_commit_not_tag_object_sha() -> None:
    source = _compose(
        PackageEntry(name="demo", source="acme/demo", ref="v2.1.0"),
        _resolved(sha="a" * 40, commit_sha=SHA),
    )["plugins"][0]["source"]
    assert source["sha"] == SHA
    assert source["ref"] == "v2.1.0"


def test_copilot_preserves_resolved_branch_ref() -> None:
    source = _compose(
        PackageEntry(name="demo", source="acme/demo", ref="release/stable"),
        _resolved(ref="release/stable"),
    )["plugins"][0]["source"]
    assert source["ref"] == "release/stable"
    assert source["sha"] == SHA


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"source_repo": ""}, "unrepresentable remote source"),
        ({"source_repo": "./invented"}, "unrepresentable remote source"),
        ({"sha": "abc123"}, "full 40-character commit SHA"),
        ({"ref": "b" * 40}, "conflicting commit pins"),
        ({"subdir": "/absolute"}, "relative repository subdirectory"),
        ({"subdir": "C:/absolute"}, "relative repository subdirectory"),
        ({"subdir": "../outside"}, "relative repository subdirectory"),
        ({"subdir": r"plugins\demo"}, "relative repository subdirectory"),
    ],
)
def test_copilot_unrepresentable_input_fails_explicitly(overrides: dict, error: str) -> None:
    with pytest.raises(BuildError, match=error):
        _compose(PackageEntry(name="demo", source="acme/demo"), _resolved(**overrides))


def test_copilot_mapper_omits_empty_description() -> None:
    doc = _compose(
        PackageEntry(name="demo", source="./plugins/demo", is_local=True),
        _resolved(source_repo="", ref="", sha=""),
    )
    assert "description" not in doc["plugins"][0]


def test_copilot_missing_configured_package_fails() -> None:
    with pytest.raises(BuildError, match="no configured source"):
        _compose(
            PackageEntry(name="other", source="./plugins/other", is_local=True),
            _resolved(),
        )


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("bad", "must be a mapping"),
        ({"extra": True}, r"Unknown key\(s\) in copilot"),
        ({"output": "   "}, "must be a non-empty string"),
        ({"output": 42}, "must be a non-empty string"),
        ({"output": "../escape.json"}, "traversal"),
    ],
)
def test_copilot_config_rejects_invalid_output(config: object, error: str) -> None:
    with pytest.raises(MarketplaceYmlError, match=error):
        _parse_copilot(config)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, ".github/plugin/marketplace.json"),
        ({}, ".github/plugin/marketplace.json"),
        ({"output": " dist/copilot.json "}, "dist/copilot.json"),
    ],
)
def test_copilot_config_valid_output(config: dict | None, expected: str) -> None:
    assert _parse_copilot(config).output == expected


@pytest.mark.parametrize(
    ("description", "version", "expected_description", "expected_version"),
    [
        (None, None, "Remote description", "2.0.0"),
        ("Curator description", "3.0.0", "Curator description", "3.0.0"),
        (None, "^2.0.0", "Remote description", "2.0.0"),
    ],
)
def test_copilot_remote_metadata_precedence(
    description: str | None,
    version: str | None,
    expected_description: str,
    expected_version: str,
) -> None:
    plugin = _compose(
        PackageEntry(name="demo", source="acme/demo", description=description, version=version),
        _resolved(),
        {"demo": {"description": "Remote description", "version": "2.0.0"}},
    )["plugins"][0]
    assert plugin["description"] == expected_description
    assert plugin["version"] == expected_version
