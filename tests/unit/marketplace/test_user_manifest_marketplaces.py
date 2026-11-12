"""User-manifest ``marketplaces:`` declaration, origin tracking and reconcile (#3006)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.marketplace import registry as registry_mod
from apm_cli.marketplace.errors import ManualMarketplaceConflictError, MarketplaceFetchError
from apm_cli.marketplace.models import (
    ORIGIN_MANIFEST,
    ORIGIN_MANUAL,
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.reconcile import (
    reconcile_user_marketplaces,
    reconcile_user_marketplaces_for_install,
)
from apm_cli.marketplace.user_manifest import (
    DeclaredMarketplace,
    parse_declared_marketplaces,
    validate_marketplaces_declaration,
)
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache


@pytest.fixture(autouse=True)
def _isolate_user_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".apm").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("APM_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(home / ".apm"))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(home / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    clear_apm_yml_cache()
    yield home
    clear_apm_yml_cache()


@pytest.fixture
def user_manifest(_isolate_user_root):
    return _isolate_user_root / ".apm" / "apm.yml"


def _write_manifest(path: Path, entries: list[dict] | None) -> None:
    lines = ["name: me", "version: 1.0.0"]
    if entries is not None:
        lines.append("marketplaces:")
        for entry in entries:
            first = True
            for key, value in entry.items():
                prefix = "  - " if first else "    "
                lines.append(f"{prefix}{key}: {value}")
                first = False
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _manifest(count: int = 2) -> MarketplaceManifest:
    plugins = tuple(MarketplacePlugin(name=f"p{i}") for i in range(count))
    return MarketplaceManifest(name="m", plugins=plugins)


def _fake_probe(name, url, ref, *, is_direct_url):
    return (
        MarketplaceSource(
            name=name, url=url, ref="" if is_direct_url else ref, path="marketplace.json"
        ),
        _manifest(),
    )


@pytest.fixture
def probe(monkeypatch):
    mock = MagicMock(side_effect=_fake_probe)
    monkeypatch.setattr("apm_cli.marketplace.reconcile.probe_registration", mock)
    return mock


def _registry_json(home: Path) -> dict:
    return json.loads((home / ".apm" / "marketplaces.json").read_text(encoding="utf-8"))


# -- declaration parsing --------------------------------------------------------


class TestParseDeclaredMarketplaces:
    def test_accepts_add_source_forms(self, user_manifest):
        data = {
            "marketplaces": [
                {"name": "acme", "source": "acme-org/agent-marketplace"},
                {"name": "tools", "source": "https://gitlab.com/g/tools#v1.0.0"},
                {"name": "ssh", "source": "git@gitea.example.com:org/repo.git", "ref": "v2"},
            ]
        }
        assert parse_declared_marketplaces(data, user_manifest) == [
            DeclaredMarketplace("acme", "acme-org/agent-marketplace"),
            DeclaredMarketplace("tools", "https://gitlab.com/g/tools#v1.0.0"),
            DeclaredMarketplace("ssh", "git@gitea.example.com:org/repo.git", "v2"),
        ]

    def test_absent_key_is_empty(self, user_manifest):
        assert parse_declared_marketplaces({"name": "x"}, user_manifest) == []

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            ("acme-org/repo", "expected a list"),
            (["acme-org/repo"], "expected a mapping"),
            ([{"name": "acme"}], "'name' and 'source' are required"),
            ([{"source": "a/b"}], "'name' and 'source' are required"),
            ([{"name": "bad name", "source": "a/b"}], "must only contain"),
            ([{"name": "a", "source": "a/b", "branch": "x"}], "unknown key(s) branch"),
            ([{"name": "a", "source": "http://example.com/a/b"}], "Insecure HTTP"),
            ([{"name": "a", "source": "https://github.com/o/r#v1", "ref": "v2"}], "one ref source"),
            (
                [{"name": "a", "source": "o/r"}, {"name": "A", "source": "o/s"}],
                "duplicate name 'A'",
            ),
        ],
    )
    def test_rejects_malformed_entries(self, user_manifest, raw, fragment):
        with pytest.raises(ValueError) as exc:
            parse_declared_marketplaces({"marketplaces": raw}, user_manifest)
        assert fragment in str(exc.value)
        assert user_manifest.as_posix() in str(exc.value)


class TestScopeRejection:
    def test_project_manifest_is_rejected_naming_user_manifest(self, tmp_path, user_manifest):
        project = tmp_path / "proj"
        project.mkdir()
        apm_yml = project / "apm.yml"
        _write_manifest(apm_yml, [{"name": "acme", "source": "acme-org/m"}])
        with pytest.raises(ValueError) as exc:
            APMPackage.from_apm_yml(apm_yml)
        message = str(exc.value)
        assert "Project and package manifests cannot declare marketplaces" in message
        assert user_manifest.as_posix() in message
        assert "apm install -g" in message

    def test_empty_key_is_still_rejected_outside_user_manifest(self, tmp_path):
        with pytest.raises(ValueError, match="cannot declare marketplaces"):
            validate_marketplaces_declaration({"marketplaces": None}, tmp_path / "apm.yml")

    def test_dependency_manifest_is_rejected(self, _isolate_user_root):
        dep = _isolate_user_root / ".apm" / "apm_modules" / "o" / "r"
        dep.mkdir(parents=True)
        _write_manifest(dep / "apm.yml", [{"name": "acme", "source": "acme-org/m"}])
        with pytest.raises(ValueError, match="cannot declare marketplaces"):
            APMPackage.from_apm_yml(dep / "apm.yml")

    def test_user_manifest_parses_without_unknown_key_warning(self, user_manifest, capsys):
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        pkg = APMPackage.from_apm_yml(user_manifest)
        assert pkg.name == "me"
        assert "Unknown top-level" not in capsys.readouterr().out

    def test_user_manifest_follows_apm_home(self, tmp_path, monkeypatch):
        apm_home = tmp_path / "custom-apm-home"
        apm_home.mkdir()
        monkeypatch.setenv("APM_HOME", str(apm_home))
        declared = {"marketplaces": [{"name": "acme", "source": "acme-org/m"}]}
        validate_marketplaces_declaration(declared, apm_home / "apm.yml")
        with pytest.raises(ValueError) as exc:
            validate_marketplaces_declaration(declared, tmp_path / "home" / ".apm" / "apm.yml")
        assert (apm_home / "apm.yml").as_posix() in str(exc.value)

    def test_user_manifest_shape_errors_surface_on_parse(self, user_manifest):
        _write_manifest(user_manifest, [{"name": "acme"}])
        with pytest.raises(ValueError, match="'name' and 'source' are required"):
            APMPackage.from_apm_yml(user_manifest)


# -- origin persistence -----------------------------------------------------------


class TestOrigin:
    def test_manual_origin_is_default_and_omitted_from_json(self):
        src = MarketplaceSource(name="acme", owner="o", repo="r")
        assert src.origin == ORIGIN_MANUAL
        assert "origin" not in src.to_dict()

    def test_manifest_origin_round_trips(self):
        src = MarketplaceSource(name="acme", owner="o", repo="r", origin=ORIGIN_MANIFEST)
        data = src.to_dict()
        assert data["origin"] == "manifest"
        assert MarketplaceSource.from_dict(data).origin == ORIGIN_MANIFEST

    def test_unknown_persisted_origin_degrades_to_manual(self):
        data = {"name": "acme", "url": "https://github.com/o/r", "origin": "elsewhere"}
        assert MarketplaceSource.from_dict(data).origin == ORIGIN_MANUAL

    def test_invalid_origin_is_rejected_programmatically(self):
        with pytest.raises(ValueError, match="Invalid marketplace origin"):
            MarketplaceSource(name="acme", owner="o", repo="r", origin="bogus")


class TestRegistryOwnership:
    def test_manifest_registration_never_overwrites_manual(self, _isolate_user_root):
        registry_mod.add_marketplace(MarketplaceSource(name="acme", owner="manual", repo="r"))
        with pytest.raises(ManualMarketplaceConflictError):
            registry_mod.register_manifest_marketplace(
                MarketplaceSource(name="ACME", owner="decl", repo="r", origin=ORIGIN_MANIFEST)
            )
        assert registry_mod.get_marketplace_by_name("acme").owner == "manual"

    def test_unregister_manifest_skips_manual(self, _isolate_user_root):
        registry_mod.add_marketplace(MarketplaceSource(name="acme", owner="o", repo="r"))
        assert registry_mod.unregister_manifest_marketplace("acme") is False
        assert registry_mod.marketplace_names() == ["acme"]

    def test_register_requires_manifest_origin(self):
        with pytest.raises(ValueError, match="manifest-origin"):
            registry_mod.register_manifest_marketplace(
                MarketplaceSource(name="acme", owner="o", repo="r")
            )


# -- reconcile --------------------------------------------------------------------


class TestReconcile:
    def test_registers_missing_entries_with_manifest_origin(self, user_manifest, probe):
        _write_manifest(
            user_manifest, [{"name": "acme", "source": "https://github.com/acme-org/m#v1"}]
        )
        logger = MagicMock()
        report = reconcile_user_marketplaces(user_manifest, logger=logger)
        assert report.registered == ["acme"]
        stored = registry_mod.get_marketplace_by_name("acme")
        assert stored.origin == ORIGIN_MANIFEST
        assert stored.url == "https://github.com/acme-org/m"
        assert stored.ref == "v1"
        assert _registry_json(user_manifest.parent.parent)["marketplaces"][0]["origin"] == (
            "manifest"
        )
        logger.success.assert_any_call("Registered marketplace 'acme' (2 plugins)", symbol="check")

    def test_unchanged_entries_are_kept_without_network(self, user_manifest, probe):
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        probe.reset_mock()
        logger = MagicMock()
        report = reconcile_user_marketplaces(user_manifest, logger=logger)
        assert report.kept == ["acme"]
        assert report.registered == report.updated == report.removed == []
        probe.assert_not_called()
        logger.progress.assert_any_call("Kept marketplace(s) unchanged: acme")

    def test_changed_entries_are_updated(self, user_manifest, probe):
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m", "ref": "v1"}])
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m", "ref": "v2"}])
        report = reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        assert report.updated == ["acme"]
        assert registry_mod.get_marketplace_by_name("acme").ref == "v2"

    def test_removes_only_manifest_origin_entries(self, user_manifest, probe):
        registry_mod.add_marketplace(MarketplaceSource(name="manual", owner="o", repo="r"))
        _write_manifest(
            user_manifest,
            [{"name": "acme", "source": "acme-org/m"}, {"name": "tools", "source": "t/t"}],
        )
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        report = reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        assert report.removed == ["tools"]
        assert registry_mod.marketplace_names() == ["acme", "manual"]

    def test_dropping_the_key_removes_manifest_entries(self, user_manifest, probe):
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        _write_manifest(user_manifest, None)
        report = reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        assert report.removed == ["acme"]
        assert registry_mod.marketplace_names() == []

    def test_manual_name_conflict_is_reported_not_overwritten(self, user_manifest, probe):
        registry_mod.add_marketplace(MarketplaceSource(name="acme", owner="manual", repo="r"))
        _write_manifest(user_manifest, [{"name": "acme", "source": "decl/m"}])
        logger = MagicMock()
        report = reconcile_user_marketplaces(user_manifest, logger=logger)
        assert report.conflicts == ["acme"]
        assert report.ok
        stored = registry_mod.get_marketplace_by_name("acme")
        assert (stored.owner, stored.origin) == ("manual", ORIGIN_MANUAL)
        probe.assert_not_called()
        warning = logger.warning.call_args.args[0]
        assert "apm marketplace remove acme" in warning

    def test_fetch_failure_is_reported_and_keeps_registry(self, user_manifest, monkeypatch):
        monkeypatch.setattr(
            "apm_cli.marketplace.reconcile.probe_registration",
            MagicMock(side_effect=MarketplaceFetchError("acme", "404")),
        )
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        logger = MagicMock()
        assert (
            reconcile_user_marketplaces_for_install(user_manifest, logger=logger, dry_run=False)
            is False
        )
        assert registry_mod.marketplace_names() == []
        final = logger.error.call_args.args[0]
        assert "acme" in final and user_manifest.as_posix() in final

    def test_malformed_declaration_fails_install(self, user_manifest, probe):
        _write_manifest(user_manifest, [{"name": "acme"}])
        logger = MagicMock()
        assert (
            reconcile_user_marketplaces_for_install(user_manifest, logger=logger, dry_run=False)
            is False
        )
        assert "'name' and 'source' are required" in logger.error.call_args.args[0]

    def test_dry_run_reports_without_writing(self, user_manifest, probe):
        _write_manifest(user_manifest, [{"name": "tools", "source": "t/t"}])
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        probe.reset_mock()
        logger = MagicMock()
        report = reconcile_user_marketplaces(user_manifest, logger=logger, dry_run=True)
        assert (report.registered, report.removed) == (["acme"], ["tools"])
        probe.assert_not_called()
        assert registry_mod.marketplace_names() == ["tools"]
        logger.dry_run_notice.assert_any_call("Would register marketplace 'acme'")
        logger.dry_run_notice.assert_any_call("Would remove marketplace 'tools'")

    def test_silent_when_nothing_declared_or_owned(self, user_manifest, probe):
        registry_mod.add_marketplace(MarketplaceSource(name="manual", owner="o", repo="r"))
        _write_manifest(user_manifest, None)
        logger = MagicMock()
        report = reconcile_user_marketplaces(user_manifest, logger=logger)
        assert report.ok
        logger.start.assert_not_called()
        assert registry_mod.marketplace_names() == ["manual"]

    @pytest.mark.parametrize("body", ["", "- a\n- b\n", "just-a-string\n", "name: [unclosed\n"])
    def test_malformed_root_fails_before_touching_registry(self, user_manifest, probe, body):
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        user_manifest.write_text(body, encoding="utf-8")
        logger = MagicMock()
        assert (
            reconcile_user_marketplaces_for_install(user_manifest, logger=logger, dry_run=False)
            is False
        )
        assert registry_mod.marketplace_names() == ["acme"]
        assert user_manifest.as_posix() in logger.error.call_args.args[0]

    def test_failed_fetch_keeps_stale_manifest_entries(self, user_manifest, monkeypatch, probe):
        _write_manifest(user_manifest, [{"name": "old", "source": "acme-org/m"}])
        reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        probe.side_effect = MarketplaceFetchError("new", "404")
        _write_manifest(user_manifest, [{"name": "new", "source": "acme-org/m2"}])
        report = reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        assert [name for name, _ in report.failed] == ["new"]
        assert report.removed == []
        assert registry_mod.marketplace_names() == ["old"]

    def test_dry_run_creates_no_registry_files(self, _isolate_user_root, user_manifest, probe):
        apm_dir = _isolate_user_root / ".apm"
        _write_manifest(user_manifest, [{"name": "acme", "source": "acme-org/m"}])
        report = reconcile_user_marketplaces(user_manifest, logger=MagicMock(), dry_run=True)
        assert report.registered == ["acme"]
        assert not (apm_dir / "marketplaces.json").exists()
        assert not (apm_dir / "config.json").exists()

    def test_nothing_declared_creates_no_registry_files(self, _isolate_user_root, user_manifest):
        apm_dir = _isolate_user_root / ".apm"
        _write_manifest(user_manifest, None)
        report = reconcile_user_marketplaces(user_manifest, logger=MagicMock())
        assert report.ok
        assert not (apm_dir / "marketplaces.json").exists()
        assert not (apm_dir / "config.json").exists()


# -- shared SOURCE grammar ------------------------------------------------------


class TestMarketplaceAddSharesRegistrationParser:
    """``apm marketplace add`` and the reconcile parse SOURCE through one owner."""

    def _invoke(self, args):
        from click.testing import CliRunner

        from apm_cli.commands.marketplace import marketplace

        return CliRunner().invoke(marketplace, ["add", *args])

    def test_add_routes_source_through_parse_registration_source(self, monkeypatch):
        calls = []

        def _boom(source, *, ref=None, host=None):
            calls.append((source, ref, host))
            raise ValueError("rejected by the shared parser")

        monkeypatch.setattr("apm_cli.marketplace.registration.parse_registration_source", _boom)
        result = self._invoke(["https://example.com/o/r.git#v1", "--branch", "dev"])
        assert result.exit_code == 1
        assert calls == [("https://example.com/o/r.git#v1", "dev", None)]
        assert "rejected by the shared parser" in result.output

    def test_add_and_declaration_reject_the_same_fragment_conflict(self, user_manifest):
        result = self._invoke(["https://example.com/o/r.git#v1", "--ref", "v2"])
        assert result.exit_code == 1
        assert "Do not combine a git URL #ref with --ref" in result.output
        with pytest.raises(ValueError, match="Do not combine a git URL #ref with --ref"):
            parse_declared_marketplaces(
                {
                    "marketplaces": [
                        {"name": "a", "source": "https://example.com/o/r.git#v1", "ref": "v2"}
                    ]
                },
                user_manifest,
            )
