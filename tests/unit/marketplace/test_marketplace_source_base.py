from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.migration import load_marketplace_config
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.yml_editor import add_plugin_entry
from apm_cli.marketplace.yml_schema import MarketplaceConfig

_SHA = "a" * 40


class _MockRefResolver:
    """In-process RefResolver stub keyed by composed ``owner/repo`` path.

    The version-range branch of ``MarketplaceBuilder._resolve_entry`` calls
    ``list_remote_refs(owner_repo)`` where ``owner_repo`` is the base-composed
    coordinate (e.g. ``platform/marketplaces/team/tool``). Keying the stub on
    that composed path proves the base composition reaches the resolver on the
    ``version:`` branch, not just the explicit-``ref`` branch.
    """

    def __init__(self, refs_by_remote: dict[str, list[RemoteRef]]) -> None:
        self._refs = refs_by_remote

    def list_remote_refs(self, owner_repo: str) -> list[RemoteRef]:
        return self._refs.get(owner_repo, [])

    def close(self) -> None:
        pass


class _RecordingAuthResolver:
    """Record AuthResolver calls while returning a token-bearing context."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, host: str, org: str | None = None):
        self.calls.append((host, org))
        return SimpleNamespace(token="token", source="test-auth")


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def _apm_yml(source_base: str | None, packages: str) -> str:
    lines = [
        "name: source-base-marketplace",
        "description: Source base marketplace",
        "version: 1.0.0",
        "marketplace:",
        "  owner:",
        "    name: ACME",
    ]
    if source_base is not None:
        lines.append(f"  sourceBase: {source_base}")
    lines.append("  packages:")
    lines.append(textwrap.indent(textwrap.dedent(packages).strip(), "    "))
    return "\n".join(lines) + "\n"


def _load_config(tmp_path: Path, source_base: str | None, packages: str) -> MarketplaceConfig:
    _write(tmp_path / "apm.yml", _apm_yml(source_base, packages))
    return load_marketplace_config(tmp_path)


class TestSourceBaseSchema:
    def test_accepts_single_and_nested_relative_sources_when_source_base_is_set(
        self, tmp_path: Path
    ) -> None:
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces/",
            f"""
            - name: single
              source: single-tool
              ref: {_SHA}
            - name: nested
              source: team/tools/nested-tool
              ref: {_SHA}
            """,
        )

        assert config.source_base == "https://gitlab.example.com/platform/marketplaces"
        assert config.packages[0].source == "single-tool"
        assert config.packages[0].host is None
        assert config.packages[1].source == "team/tools/nested-tool"
        assert config.packages[1].host is None

    def test_absent_source_base_keeps_owner_repo_source_unchanged(self, tmp_path: Path) -> None:
        config = _load_config(
            tmp_path,
            None,
            f"""
            - name: existing
              source: owner/repo
              ref: {_SHA}
            """,
        )

        assert config.source_base is None
        assert config.packages[0].source == "owner/repo"
        assert config.packages[0].host is None

    @pytest.mark.parametrize(
        ("source_base", "message"),
        [
            ("http://gitlab.example.com/group", "https"),
            ("https://user@gitlab.example.com/group", "userinfo"),
            ("https://gitlab.example.com:443/group", "port"),
            ("https://gitlab.example.com/group?token=x", "query"),
            ("https://gitlab.example.com/group#frag", "fragment"),
            ("https://gitlab.example.com/group.git", r"\.git"),
            ("https://localhost/group", "FQDN"),
            ("https://gitlab.example.com/group//repo", "empty"),
            ("https://gitlab.example.com/group//", "empty"),
            ("https://gitlab.example.com/group/../repo", "traversal"),
        ],
    )
    def test_rejects_source_base_security_guard_violations(
        self, tmp_path: Path, source_base: str, message: str
    ) -> None:
        with pytest.raises(MarketplaceYmlError, match=message):
            _load_config(
                tmp_path,
                source_base,
                f"""
                - name: tool
                  source: tool
                  ref: {_SHA}
                """,
            )

    def test_rejects_single_segment_source_without_source_base(self, tmp_path: Path) -> None:
        with pytest.raises(MarketplaceYmlError, match="must be one of"):
            _load_config(
                tmp_path,
                None,
                f"""
                - name: tool
                  source: tool
                  ref: {_SHA}
                """,
            )

    @pytest.mark.parametrize(
        "source",
        [
            "team.tools/owner/repo/extra",
            "gitlab.example.com/owner/repo/extra",
        ],
    )
    def test_rejects_relative_source_with_fqdn_first_segment_when_source_base_is_set(
        self, tmp_path: Path, source: str
    ) -> None:
        # A value with a FQDN-like first segment that forms neither a valid
        # owner/repo nor host/owner/repo override shape is rejected even with
        # sourceBase set, rather than being silently composed onto the base:
        # that disambiguation avoids a confused-deputy footgun
        # (manifest-schema.md Section 7.5).
        with pytest.raises(MarketplaceYmlError, match="host-prefixed source"):
            _load_config(
                tmp_path,
                "https://gitlab.example.com/platform/marketplaces",
                f"""
                - name: tool
                  source: {source}
                  ref: {_SHA}
                """,
            )

    def test_rejects_host_looking_relative_source_with_actionable_message(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(MarketplaceYmlError, match="looks like a host-prefixed source"):
            _load_config(
                tmp_path,
                "https://gitlab.example.com/platform/marketplaces",
                f"""
                - name: tool
                  source: gitlab.example.com/team/tool/extra
                  ref: {_SHA}
                """,
            )


class TestSourceBaseBuildComposition:
    def test_composes_relative_source_onto_base_for_resolution_and_output(
        self, tmp_path: Path
    ) -> None:
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces",
            f"""
            - name: tool
              source: team/tool
              ref: {_SHA}
            """,
        )
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))

        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == "platform/marketplaces/team/tool"
        assert resolved.host == "gitlab.example.com"
        parsed = urlparse(resolved.source_url or "")
        assert parsed.scheme == "https"
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team/tool"

        doc = builder.compose_marketplace_json([resolved])
        source = doc["plugins"][0]["source"]
        assert source["source"] == "url"
        parsed = urlparse(source["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team/tool"

    def test_host_prefixed_source_overrides_source_base(self, tmp_path: Path) -> None:
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces",
            f"""
            - name: override
              source: ghe.example.com/acme/tool
              ref: {_SHA}
            """,
        )
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))

        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == "acme/tool"
        assert resolved.host == "ghe.example.com"
        assert resolved.source_url is None

        doc = builder.compose_marketplace_json([resolved])
        source = doc["plugins"][0]["source"]
        assert source["source"] == "url"
        parsed = urlparse(source["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname == "ghe.example.com"
        assert parsed.path == "/acme/tool"

    def test_composes_relative_source_onto_base_for_version_range_resolution(
        self, tmp_path: Path
    ) -> None:
        # Lock the compose-onto-base behavior for the ``version:`` branch too.
        # The explicit-``ref`` test exercises one branch of ``_resolve_entry``;
        # ``_resolve_version_range`` threads the same source_host/source_url and
        # must select a tag against the composed ``owner/repo`` coordinate.
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces",
            """
            - name: tool
              source: team/tool
              version: "^1.0.0"
            """,
        )
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
        composed_repo = "platform/marketplaces/team/tool"
        refs = {
            composed_repo: [
                RemoteRef(name="refs/tags/v1.0.0", sha="b" * 40),
                RemoteRef(name="refs/tags/v1.2.0", sha="c" * 40),
                RemoteRef(name="refs/tags/v2.0.0", sha="d" * 40),
            ]
        }
        builder._get_resolver_for_host = (  # type: ignore[assignment]
            lambda _host, **_kwargs: _MockRefResolver(refs)
        )

        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == composed_repo
        assert resolved.host == "gitlab.example.com"
        assert resolved.ref == "v1.2.0"
        assert resolved.sha == "c" * 40
        parsed = urlparse(resolved.source_url or "")
        assert parsed.scheme == "https"
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team/tool"

        doc = builder.compose_marketplace_json([resolved])
        source = doc["plugins"][0]["source"]
        assert source["source"] == "url"
        parsed = urlparse(source["url"])
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team/tool"

    def test_ado_shaped_source_base_composes_relative_repo(self, tmp_path: Path) -> None:
        # Forward-compat guard for the #1010 / future-ADO reuse: an Azure DevOps
        # base (``org/project/_git`` 3-part path, underscore-leading ``_git``
        # segment) must validate and compose a relative repo onto it. ADO will
        # REUSE sourceBase rather than add a separate ``host`` field, so the
        # field shape must not be narrowed to reject this base.
        config = _load_config(
            tmp_path,
            "https://dev.azure.com/contoso/platform/_git",
            f"""
            - name: ado-tool
              source: agent-skills
              ref: {_SHA}
            """,
        )
        assert config.source_base == "https://dev.azure.com/contoso/platform/_git"
        assert config.packages[0].source == "agent-skills"
        assert config.packages[0].host is None

        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == "contoso/platform/_git/agent-skills"
        assert resolved.host == "dev.azure.com"
        parsed = urlparse(resolved.source_url or "")
        assert parsed.scheme == "https"
        assert parsed.hostname == "dev.azure.com"
        assert parsed.path == "/contoso/platform/_git/agent-skills"

    def test_dotted_subgroup_relative_source_composes_onto_base(self, tmp_path: Path) -> None:
        # A relative source whose owner segment legitimately contains a dot
        # (e.g. a GitLab subgroup named ``team.tools``) is a valid two-segment
        # ``owner/repo`` shape and MUST compose onto the base -- it is NOT a
        # host-looking value the confused-deputy guard rejects. Locks the
        # accepted behavior so future guard changes cannot silently start
        # blocking valid dotted subgroup names.
        config = _load_config(
            tmp_path,
            "https://gitlab.example.com/platform/marketplaces",
            f"""
            - name: dotted
              source: team.tools/repo
              ref: {_SHA}
            """,
        )
        assert config.packages[0].source == "team.tools/repo"
        assert config.packages[0].host is None

        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
        resolved = builder._resolve_entry(config.packages[0])
        assert resolved.source_repo == "platform/marketplaces/team.tools/repo"
        assert resolved.host == "gitlab.example.com"
        parsed = urlparse(resolved.source_url or "")
        assert parsed.hostname == "gitlab.example.com"
        assert parsed.path == "/platform/marketplaces/team.tools/repo"

    def test_source_base_resolution_uses_base_org_for_auth_context(self, tmp_path: Path) -> None:
        config = _load_config(
            tmp_path,
            "https://github.com/contoso/marketplaces",
            f"""
            - name: tool
              source: tool
              ref: {_SHA}
            """,
        )
        auth = _RecordingAuthResolver()
        builder = MarketplaceBuilder.from_config(
            config,
            tmp_path,
            BuildOptions(offline=False),
            auth_resolver=auth,
        )

        resolved = builder._resolve_entry(config.packages[0])

        assert resolved.source_repo == "contoso/marketplaces/tool"
        assert auth.calls == [("github.com", "contoso")]


class TestSourceBaseEditor:
    def test_add_plugin_entry_accepts_relative_source_when_source_base_is_set(
        self, tmp_path: Path
    ) -> None:
        yml_path = _write(
            tmp_path / "apm.yml",
            _apm_yml(
                "https://gitlab.example.com/platform/marketplaces",
                f"""
                - name: existing
                  source: existing-tool
                  ref: {_SHA}
                """,
            ),
        )

        name = add_plugin_entry(yml_path, source="new-tool", ref=_SHA)

        assert name == "new-tool"
        config = load_marketplace_config(tmp_path)
        added = next(pkg for pkg in config.packages if pkg.name == "new-tool")
        assert added.source == "new-tool"
        assert added.host is None
