"""Tests for the top-level ``registries:`` block in apm.yml.

Covers ``APMPackage.from_apm_yml`` parsing of the new block plus the
default-registry routing pass per docs/proposals/registry-api.md §3.1/§3.2.

The hardest invariant: a project without a ``registries:`` block is
byte-identical to pre-PR behavior (invariant §2.1.1).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


@pytest.fixture
def write_yml(tmp_path):
    """Yield a helper that writes ``apm.yml`` content to a temp dir."""

    def _write(content: str) -> Path:
        clear_apm_yml_cache()
        p = tmp_path / "apm.yml"
        p.write_text(textwrap.dedent(content).strip() + "\n")
        return p

    return _write


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    """Most tests in this module exercise the experimental registry feature."""
    import apm_cli.config as _conf

    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {"experimental": {"registries": True}},
    )


def _disable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    clear_apm_yml_cache()
    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {}})


# ───────────────────────────────────────────────────────────────────────────
# Block parsing
# ───────────────────────────────────────────────────────────────────────────


class TestRegistriesBlockParsing:
    def test_no_block_means_no_change(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#main
                - acme/bar#v1.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries is None
        assert pkg.default_registry is None
        for dep in pkg.dependencies["apm"]:
            assert dep.source is None

    def test_block_without_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-main:
                url: https://corp.example.com/apm
            dependencies:
              apm:
                - acme/foo#v1.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries == {"corp-main": "https://corp.example.com/apm"}
        assert pkg.default_registry is None
        # Without a default, plain shorthand stays on Git.
        assert pkg.dependencies["apm"][0].source is None

    def test_block_with_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.default_registry == "corp"
        assert "corp" in pkg.registries

    def test_multiple_registries(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-a:
                url: https://a.example.com/apm
              corp-b:
                url: https://b.example.com/apm
              default: corp-a
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert set(pkg.registries.keys()) == {"corp-a", "corp-b"}
        assert pkg.default_registry == "corp-a"

    def test_empty_block(self, write_yml):
        # ``registries: {}`` is harmless and yields no fields set.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries: {}
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries is None
        assert pkg.default_registry is None


class TestPackageRegistryExperimentalGate:
    def test_non_empty_registries_block_requires_flag(self, write_yml, monkeypatch):
        _disable_package_registry(monkeypatch)
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
            """)
        with pytest.raises(ValueError, match="apm experimental enable registries"):
            APMPackage.from_apm_yml(p)

    def test_empty_registries_block_allowed_without_flag(self, write_yml, monkeypatch):
        _disable_package_registry(monkeypatch)
        p = write_yml("""
            name: x
            version: 1.0.0
            registries: {}
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries is None
        assert pkg.default_registry is None


class TestRegistriesBlockValidation:
    def test_non_mapping_block_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries: not-a-mapping
            """)
        with pytest.raises(ValueError, match="must be a mapping"):
            APMPackage.from_apm_yml(p)

    def test_missing_url_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp: {}
            """)
        with pytest.raises(ValueError, match="missing required field 'url:'"):
            APMPackage.from_apm_yml(p)

    def test_non_http_url_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: ftp://corp.example.com
            """)
        with pytest.raises(ValueError, match="https:// or http://"):
            APMPackage.from_apm_yml(p)

    def test_token_field_rejected(self, write_yml):
        # Tokens must never appear in apm.yml — use env vars or config.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
                token: oops-not-here
            """)
        with pytest.raises(ValueError, match=r"must not appear in apm\.yml"):
            APMPackage.from_apm_yml(p)

    def test_unknown_default_rejected(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com
              default: nonexistent
            """)
        with pytest.raises(ValueError, match="unconfigured registry"):
            APMPackage.from_apm_yml(p)


_V01_SCHEMA = "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.schema.json"
_CONTRACTS = pytest.mark.parametrize(
    "schema_line", ["", f"$schema: {_V01_SCHEMA}"], ids=["default", "openapm-v0.1"]
)


def _registry_manifest(schema_line: str, entry: str) -> str:
    return f"""
        {schema_line}
        name: x
        version: 1.0.0
        registries:
          corp:
            {entry}
        """


class TestRegistryUrlScheme:
    """req-sc-006: plain http:// is a parse error unless local or insecure: true (#4286)."""

    @_CONTRACTS
    @pytest.mark.parametrize(
        "url",
        ["http://registry.example.com/apm", "HTTP://registry.example.com", "http://8.8.8.8/apm"],
    )
    def test_http_to_public_host_is_a_parse_error(self, write_yml, schema_line, url):
        p = write_yml(_registry_manifest(schema_line, f"url: {url}"))
        with pytest.raises(ValueError, match=r"Registry 'corp' uses http:// to a non-local host"):
            APMPackage.from_apm_yml(p)

    @_CONTRACTS
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/apm",
            "http://127.0.0.1:8080/apm",
            "http://[::1]:8080/apm",
            "http://10.1.2.3/apm",
            "http://172.16.0.9/apm",
            "http://192.168.1.10/apm",
            "https://registry.example.com/apm",
        ],
    )
    def test_https_loopback_and_private_hosts_are_accepted(self, write_yml, schema_line, url):
        p = write_yml(_registry_manifest(schema_line, f"url: {url}"))
        assert APMPackage.from_apm_yml(p).registries["corp"] == url

    @_CONTRACTS
    def test_insecure_true_accepts_http_to_public_host(self, write_yml, schema_line):
        url = "http://registry.example.com/apm"
        p = write_yml(_registry_manifest(schema_line, f"url: {url}\n            insecure: true"))
        assert APMPackage.from_apm_yml(p).registries["corp"] == url

    @_CONTRACTS
    @pytest.mark.parametrize("value", ["false", '"true"', "1"])
    def test_insecure_must_be_true_boolean(self, write_yml, schema_line, value):
        entry = f"url: http://registry.example.com/apm\n            insecure: {value}"
        p = write_yml(_registry_manifest(schema_line, entry))
        with pytest.raises(ValueError, match="Registry 'corp'"):
            APMPackage.from_apm_yml(p)

    @_CONTRACTS
    @pytest.mark.parametrize(
        "url",
        [
            "https://user:FAKE_SECRET_123@[invalid/apm",
            "ftp://user:FAKE_SECRET_123@corp.example.com",
            "http://user:FAKE_SECRET_123@registry.example.com/apm",
        ],
    )
    def test_diagnostic_never_echoes_url_credentials(self, write_yml, schema_line, url):
        p = write_yml(_registry_manifest(schema_line, f"url: '{url}'"))
        with pytest.raises(ValueError, match="Registry 'corp'") as excinfo:
            APMPackage.from_apm_yml(p)
        assert "FAKE_SECRET_123" not in str(excinfo.value)

    def test_workspace_manifest_http_registry_is_not_merged(self, tmp_path, monkeypatch):
        from apm_cli.deps.registry.config_loader import _load_yaml_registries

        workspace = tmp_path / "apm.yml"
        workspace.write_text(
            "registries:\n"
            "  public: {url: 'http://registry.example.com/apm'}\n"
            "  opted: {url: 'http://registry.example.com/apm', insecure: true}\n"
            "  local: {url: 'http://127.0.0.1:9000/apm'}\n"
        )
        assert _load_yaml_registries(workspace) == {
            "opted": "http://registry.example.com/apm",
            "local": "http://127.0.0.1:9000/apm",
        }


class TestRegistryEntryKeys:
    """req-mf-015: a mistyped key is rejected and an x- key is accepted (#4252)."""

    @_CONTRACTS
    def test_mistyped_key_is_rejected_naming_key_and_registry(self, write_yml, schema_line):
        entry = "url: https://r.example.com\n            typo: 1"
        p = write_yml(_registry_manifest(schema_line, entry))
        with pytest.raises(ValueError, match=r"Registry 'corp' has unknown fields: \['typo'\]"):
            APMPackage.from_apm_yml(p)

    @_CONTRACTS
    def test_extension_key_is_accepted(self, write_yml, schema_line):
        entry = "url: https://r.example.com\n            x-acme-note: 1"
        p = write_yml(_registry_manifest(schema_line, entry))
        assert APMPackage.from_apm_yml(p).registries["corp"] == "https://r.example.com"

    @_CONTRACTS
    def test_bare_x_prefix_is_not_an_extension(self, write_yml, schema_line):
        entry = "url: https://r.example.com\n            x-: 1"
        p = write_yml(_registry_manifest(schema_line, entry))
        with pytest.raises(ValueError, match="unknown fields"):
            APMPackage.from_apm_yml(p)


# ───────────────────────────────────────────────────────────────────────────
# Default-registry routing pass
# ───────────────────────────────────────────────────────────────────────────


class TestDefaultRouting:
    def test_shorthand_routes_to_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#^1.0.0
                - acme/bar#1.2.3
            """)
        pkg = APMPackage.from_apm_yml(p)
        for dep in pkg.dependencies["apm"]:
            assert dep.source == "registry"
            assert dep.registry_name == "corp"

    @pytest.mark.xfail(reason="@registry scope shorthand not yet implemented", strict=True)
    def test_at_scope_overrides_default(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-main:
                url: https://main.example.com/apm
              corp-other:
                url: https://other.example.com/apm
              default: corp-main
            dependencies:
              apm:
                - acme/foo@corp-other#^1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.registry_name == "corp-other"

    def test_explicit_git_object_form_not_routed(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - git: https://github.com/owner/repo.git
                  ref: v2.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.source == "git"  # explicit, not "registry"

    def test_object_form_registry_preserved(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp-main:
                url: https://main.example.com/apm
              corp-other:
                url: https://other.example.com/apm
              default: corp-main
            dependencies:
              apm:
                - registry: corp-other
                  id: acme/prompts
                  path: a/b.prompt.md
                  version: 1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.registry_name == "corp-other"

    def test_local_path_not_routed(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - ./local-pkg
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.is_local
        assert d.source == "local"

    def test_dev_dependencies_routed_too(self, write_yml):
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            devDependencies:
              apm:
                - acme/foo#1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dev_dependencies["apm"][0]
        assert d.source == "registry"
        assert d.registry_name == "corp"


class TestDefaultRoutingErrors:
    def test_branch_ref_routed_to_registry(self, write_yml):
        # Routing is unconditional — any ref (including branch names) routes
        # to the default registry. Use ``- git:`` to keep a dep on Git.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#main
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.source == "registry"
        assert d.registry_name == "corp"

    def test_commit_sha_routed_to_registry(self, write_yml):
        # Commit SHAs are opaque version strings to the registry.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#abc123d
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.source == "registry"
        assert d.registry_name == "corp"

    def test_missing_ref_rejected(self, write_yml):
        # A shorthand with no ref at all is always rejected — the registry
        # requires a version selector.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo
            """)
        with pytest.raises(ValueError, match="no version constraint"):
            APMPackage.from_apm_yml(p)

    def test_non_semver_ref_routed_to_registry(self, write_yml):
        # Non-semver labels like branch names are valid registry version
        # selectors -- the client matches them exactly against the registry's
        # published version list.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#develop
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.source == "registry"
        assert d.registry_name == "corp"

    def test_malformed_range_ref_routes_without_raising_at_load(self, write_yml):
        # A malformed range-like ref ('^1.0' missing the patch) must NOT abort
        # the manifest load -- read-only consumers (apm compile, apm outdated)
        # load the manifest but never resolve, so a load-time raise would crash
        # them for an unrelated dep. The ref still routes to the registry; it is
        # rejected later, at resolve time (see the resolver tests). The CLI
        # write gate (validate_registry_ref) rejects it before it is ever
        # written to apm.yml.
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              corp:
                url: https://corp.example.com/apm
              default: corp
            dependencies:
              apm:
                - acme/foo#^1.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        d = pkg.dependencies["apm"][0]
        assert d.source == "registry"
        assert d.registry_name == "corp"


# ───────────────────────────────────────────────────────────────────────────
# Backwards-compatibility invariants
# ───────────────────────────────────────────────────────────────────────────


class TestBackwardsCompatibility:
    """A project without ``registries:`` MUST behave byte-identically to pre-PR."""

    def test_branch_ref_still_valid_without_registries(self, write_yml):
        # Per invariant §2.1.3, branch refs remain valid in the absence of
        # a registries: block — the parser stays ref-opaque on the Git path.
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#main
                - acme/foo#abc123d
            """)
        pkg = APMPackage.from_apm_yml(p)
        for dep in pkg.dependencies["apm"]:
            assert dep.source is None

    def test_no_version_still_valid_without_registries(self, write_yml):
        # `acme/foo` without a #ref is fine when no default registry is set.
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.dependencies["apm"][0].reference is None


class TestConfigJsonDefaultRegistry:
    def test_config_default_routes_without_apm_yml_block(self, write_yml, monkeypatch):
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {
                "experimental": {"registries": True},
                "registries": {
                    "corp-main": {
                        "url": "https://corp.example.com/apm",
                        "default": True,
                    }
                },
            },
        )
        p = write_yml("""
            name: x
            version: 1.0.0
            dependencies:
              apm:
                - acme/foo#^1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.registries == {"corp-main": "https://corp.example.com/apm"}
        assert pkg.default_registry == "corp-main"
        dep = pkg.dependencies["apm"][0]
        assert dep.source == "registry"
        assert dep.registry_name == "corp-main"

    def test_apm_yml_default_wins_over_config_default(self, write_yml, monkeypatch):
        import apm_cli.config as _conf

        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {
                "experimental": {"registries": True},
                "registries": {
                    "config-only": {
                        "url": "https://config.example.com/apm",
                        "default": True,
                    }
                },
            },
        )
        p = write_yml("""
            name: x
            version: 1.0.0
            registries:
              project-main:
                url: https://project.example.com/apm
              default: project-main
            dependencies:
              apm:
                - acme/foo#^1.0.0
            """)
        pkg = APMPackage.from_apm_yml(p)
        assert pkg.default_registry == "project-main"
        assert pkg.registries["project-main"] == "https://project.example.com/apm"
        assert pkg.registries["config-only"] == "https://config.example.com/apm"
        dep = pkg.dependencies["apm"][0]
        assert dep.registry_name == "project-main"
