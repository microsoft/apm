"""Explicit catalog path selection for ``apm marketplace add --path`` (#3319).

One repository can publish several catalogs, each in its own subfolder.
``--path`` selects one repository-relative ``marketplace.json`` at
registration, records it in the registration, and is admitted through the
same marketplace source-admission owner that guards persisted reloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.marketplace import client as mp_client
from apm_cli.marketplace.models import MarketplaceSource
from apm_cli.marketplace.source_identity import admit_marketplace_catalog_path

CATALOG = {"name": "frontend", "plugins": [{"name": "fe-skill", "source": "./plugins/fe"}]}


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


def _registered() -> list[dict]:
    from apm_cli.marketplace import registry

    registry._registry_cache = None
    return [s.to_dict() for s in registry.get_registered_marketplaces()]


def _invoke_add(*args: str):
    from apm_cli.commands.marketplace import marketplace

    return CliRunner().invoke(marketplace, ["add", *args])


def _two_catalog_repo(root: Path) -> Path:
    repo = root / "catalog-repo"
    for audience in ("frontend", "backend"):
        catalog = repo / "catalogs" / audience / "marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            json.dumps(
                {
                    "name": audience,
                    "plugins": [{"name": f"{audience}-skill", "source": f"./plugins/{audience}"}],
                }
            ),
            encoding="utf-8",
        )
    return repo


# ---------------------------------------------------------------------------
# Admission owner
# ---------------------------------------------------------------------------


class TestAdmitMarketplaceCatalogPath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("catalogs/frontend/marketplace.json", "catalogs/frontend/marketplace.json"),
            ("./catalogs/frontend/marketplace.json", "catalogs/frontend/marketplace.json"),
            (" marketplace.json ", "marketplace.json"),
            (".claude-plugin/marketplace.json", ".claude-plugin/marketplace.json"),
            ("catalogs/backend/catalog.JSON", "catalogs/backend/catalog.JSON"),
        ],
    )
    def test_accepts_contained_json_paths(self, raw: str, expected: str) -> None:
        assert admit_marketplace_catalog_path(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("", "empty"),
            ("   ", "empty"),
            ("../other/marketplace.json", "traversal"),
            ("catalogs/../../marketplace.json", "traversal"),
            ("catalogs/%2e%2e/%2e%2e/marketplace.json", "traversal"),
            ("catalogs/./marketplace.json", "traversal"),
            ("/etc/marketplace.json", "relative to the repository root"),
            ("C:/catalogs/marketplace.json", "relative to the repository root"),
            ("catalogs\\frontend\\marketplace.json", "forward slashes"),
            ("catalogs//marketplace.json", "empty segment"),
            ("catalogs/frontend", "JSON file"),
            ("catalogs/frontend/", "JSON file"),
            ("catalogs/\x00/marketplace.json", "control characters"),
            ("catalogs/marketplace.json?ref=main#catalog.json", "'\\?' or '#'"),
            ("catalogs/a#b.json", "'\\?' or '#'"),
            (123, "must be a string"),
        ],
    )
    def test_rejects_escaping_or_malformed_paths(self, raw: object, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            admit_marketplace_catalog_path(raw)


class TestPersistedRegistrationAdmission:
    def test_path_round_trips_through_registration(self) -> None:
        source = MarketplaceSource(
            name="frontend",
            url="https://github.com/acme/skills",
            path="catalogs/frontend/marketplace.json",
        )
        restored = MarketplaceSource.from_dict(source.to_dict())
        assert restored.path == "catalogs/frontend/marketplace.json"

    def test_reload_rejects_escaping_path(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            MarketplaceSource.from_dict(
                {
                    "name": "evil",
                    "url": "https://github.com/acme/skills",
                    "path": "../../outside/marketplace.json",
                }
            )

    def test_registry_skips_hand_edited_escaping_entry(self, tmp_path: Path) -> None:
        registry_file = tmp_path / ".apm" / "marketplaces.json"
        registry_file.parent.mkdir(parents=True)
        registry_file.write_text(
            json.dumps(
                {
                    "marketplaces": [
                        {
                            "name": "evil",
                            "url": "https://github.com/acme/skills",
                            "path": "../outside/marketplace.json",
                        },
                        {
                            "name": "good",
                            "url": "https://github.com/acme/skills",
                            "path": "catalogs/a/marketplace.json",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert [entry["name"] for entry in _registered()] == ["good"]

    def test_registry_skips_non_string_path_entry(self, tmp_path: Path) -> None:
        registry_file = tmp_path / ".apm" / "marketplaces.json"
        registry_file.parent.mkdir(parents=True)
        registry_file.write_text(
            json.dumps(
                {
                    "marketplaces": [
                        {"name": "bad", "url": "https://github.com/acme/skills", "path": 123},
                        {"name": "good", "url": "https://github.com/acme/skills"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert [entry["name"] for entry in _registered()] == ["good"]

    def test_default_and_direct_url_paths_unchanged(self) -> None:
        assert MarketplaceSource(name="a", url="https://github.com/o/r").path == "marketplace.json"
        direct = MarketplaceSource(
            name="b", url="https://catalog.example.com/marketplace.json", ref="", path=""
        )
        assert direct.path == ""
        assert direct.kind == "url"


# ---------------------------------------------------------------------------
# CLI: apm marketplace add --path
# ---------------------------------------------------------------------------


class TestMarketplaceAddPath:
    def test_registers_two_catalogs_from_one_local_repository(self, tmp_path: Path) -> None:
        repo = _two_catalog_repo(tmp_path)

        for audience in ("frontend", "backend"):
            result = _invoke_add(
                str(repo),
                "--path",
                f"catalogs/{audience}/marketplace.json",
                "--name",
                f"acme-{audience}",
            )
            assert result.exit_code == 0, result.output
            assert f"Marketplace 'acme-{audience}' registered (1 plugins)" in result.output

        registered = {entry["name"]: entry for entry in _registered()}
        assert registered["acme-frontend"]["path"] == "catalogs/frontend/marketplace.json"
        assert registered["acme-backend"]["path"] == "catalogs/backend/marketplace.json"

    def test_without_path_subfolder_catalog_is_not_discovered(self, tmp_path: Path) -> None:
        repo = _two_catalog_repo(tmp_path)

        result = _invoke_add(str(repo), "--name", "acme")

        assert result.exit_code == 1
        assert (
            "Checked: marketplace.json, .github/plugin/marketplace.json, "
            ".claude-plugin/marketplace.json" in " ".join(result.output.split())
        )
        assert _registered() == []

    def test_explicit_path_fetches_only_that_file(self) -> None:
        requested: list[str] = []

        def fake_fetch(source, file_path, **_kw):
            requested.append(file_path)
            return CATALOG if file_path == "catalogs/frontend/marketplace.json" else None

        with (
            patch.object(mp_client, "_fetch_file", side_effect=fake_fetch),
            patch.object(mp_client, "_write_cache"),
        ):
            result = _invoke_add(
                "acme/skills",
                "--ref",
                "v1.0.0",
                "--path",
                "./catalogs/frontend/marketplace.json",
                "--name",
                "acme-frontend",
            )

        assert result.exit_code == 0, result.output
        assert requested == ["catalogs/frontend/marketplace.json"]
        [entry] = _registered()
        assert entry["path"] == "catalogs/frontend/marketplace.json"
        assert entry["ref"] == "v1.0.0"

    @pytest.mark.parametrize(
        "bad_path",
        ["../outside/marketplace.json", "/abs/marketplace.json", "catalogs/frontend"],
    )
    def test_invalid_path_fails_before_any_fetch_or_registration(self, bad_path: str) -> None:
        with patch.object(mp_client, "_fetch_file") as fetch_mock:
            result = _invoke_add("acme/skills", "--ref", "v1.0.0", "--path", bad_path)

        assert result.exit_code == 1
        output = " ".join(result.output.split())
        assert f"Invalid --path '{bad_path}'" in output
        assert "for example catalogs/frontend/marketplace.json" in output
        fetch_mock.assert_not_called()
        assert _registered() == []

    def test_missing_path_fails_with_actionable_error(self, tmp_path: Path) -> None:
        repo = _two_catalog_repo(tmp_path)

        result = _invoke_add(str(repo), "--path", "catalogs/mobile/marketplace.json")

        assert result.exit_code == 1
        output = " ".join(result.output.split())
        assert "No marketplace catalog found at 'catalogs/mobile/marketplace.json'" in output
        assert "relative to the repository root" in output
        assert _registered() == []

    def test_path_rejected_for_hosted_marketplace_json_url(self) -> None:
        with patch.object(mp_client, "_fetch_file") as fetch_mock:
            result = _invoke_add(
                "https://catalog.example.com/marketplace.json",
                "--name",
                "catalog",
                "--path",
                "catalogs/frontend/marketplace.json",
            )

        assert result.exit_code == 1
        assert "--path is not supported for hosted marketplace.json URLs" in " ".join(
            result.output.split()
        )
        fetch_mock.assert_not_called()
        assert _registered() == []

    def test_path_rejected_for_local_marketplace_file(self, tmp_path: Path) -> None:
        repo = _two_catalog_repo(tmp_path)
        catalog_file = repo / "catalogs" / "frontend" / "marketplace.json"

        result = _invoke_add(str(catalog_file), "--path", "marketplace.json")

        assert result.exit_code == 1
        assert "--path is not supported when SOURCE is a marketplace.json file" in " ".join(
            result.output.split()
        )
        assert _registered() == []

    def test_refresh_reads_the_recorded_path(self, tmp_path: Path) -> None:
        repo = _two_catalog_repo(tmp_path)
        from apm_cli.commands.marketplace import marketplace

        assert (
            _invoke_add(
                str(repo), "--path", "catalogs/backend/marketplace.json", "--name", "acme-backend"
            ).exit_code
            == 0
        )
        result = CliRunner().invoke(marketplace, ["update", "acme-backend"])
        assert result.exit_code == 0, result.output
        assert "Marketplace 'acme-backend' updated (1 plugins)" in result.output

        browse = CliRunner().invoke(marketplace, ["browse", "acme-backend"])
        assert browse.exit_code == 0, browse.output
        assert "backend-skill" in browse.output
        assert "frontend-skill" not in browse.output
        [entry] = _registered()
        assert entry["path"] == "catalogs/backend/marketplace.json"

    def test_replacing_a_different_catalog_under_the_same_name_warns(self, tmp_path: Path) -> None:
        repo = _two_catalog_repo(tmp_path)
        frontend = ("--path", "catalogs/frontend/marketplace.json", "--name", "acme")

        assert _invoke_add(str(repo), *frontend).exit_code == 0
        same = _invoke_add(str(repo), *frontend)
        assert same.exit_code == 0, same.output
        assert "Replacing marketplace" not in same.output

        result = _invoke_add(
            str(repo), "--path", "catalogs/backend/marketplace.json", "--name", "acme"
        )

        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())
        assert "Replacing marketplace 'acme'" in output
        assert "path 'catalogs/frontend/marketplace.json'" in output
        assert "Use --name to keep both registrations." in output
        [entry] = _registered()
        assert entry["path"] == "catalogs/backend/marketplace.json"

    def test_re_adding_without_path_keeps_silent_replace(self, tmp_path: Path) -> None:
        for audience in ("one", "two"):
            repo = tmp_path / audience
            repo.mkdir()
            (repo / "marketplace.json").write_text(json.dumps(CATALOG), encoding="utf-8")
            result = _invoke_add(str(repo), "--name", "acme")
            assert result.exit_code == 0, result.output
            assert "Replacing marketplace" not in result.output


class TestGithubContentsUrl:
    def test_catalog_path_is_encoded_and_ref_preserved(self) -> None:
        from types import SimpleNamespace
        from urllib.parse import parse_qs, urlsplit

        source = MarketplaceSource(
            name="acme",
            url="https://github.com/acme/skills",
            ref="v1.0.0",
            path="catalogs/front end/marketplace.json",
        )
        url = mp_client._github_contents_url(
            source, source.path, SimpleNamespace(api_base="https://api.github.com")
        )

        parts = urlsplit(url)
        assert parts.path == "/repos/acme/skills/contents/catalogs/front%20end/marketplace.json"
        assert parse_qs(parts.query) == {"ref": ["v1.0.0"]}
        assert parts.fragment == ""
