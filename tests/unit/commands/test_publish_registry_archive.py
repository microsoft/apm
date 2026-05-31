"""Tests for ``apm publish`` flat registry archive packing and owner/repo resolution."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.exceptions import ClickException, UsageError

from apm_cli.commands.publish import _pack_archive, _resolve_package_id
from apm_cli.models.apm_package import APMPackage


def _list_tar_members(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return sorted(m.name for m in tar.getmembers())


class TestPackRegistryArchive:
    def test_flat_apm_yml_and_dot_apm_at_root(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(
            "name: demo\nversion: 1.2.3\ndescription: x\nauthor: a\n",
            encoding="utf-8",
        )
        skill = tmp_path / ".apm" / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: demo\n---\n\n# Demo\n", encoding="utf-8")

        pkg = APMPackage(name="demo", version="1.2.3")
        logger = MagicMock()
        tarball = _pack_archive(tmp_path, tmp_path / "apm.yml", pkg, logger, verbose=False)

        assert tarball.name == "demo-1.2.3.tar.gz"
        members = _list_tar_members(tarball.read_bytes())
        assert "apm.yml" in members
        assert any(m.startswith(".apm/skills/demo/SKILL.md") for m in members)
        assert not any("/plugin.json" in m for m in members)
        assert not any(m.startswith("demo-1.2.3/") for m in members)

    def test_skips_appledouble_sidecars(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\ndescription: x\nauthor: a\n",
            encoding="utf-8",
        )
        apm_dir = tmp_path / ".apm" / "skills" / "demo"
        apm_dir.mkdir(parents=True)
        (apm_dir / "SKILL.md").write_text("# skill\n", encoding="utf-8")
        (tmp_path / "._apm.yml").write_bytes(b"junk")
        (apm_dir / "._SKILL.md").write_bytes(b"junk")

        pkg = APMPackage(name="demo", version="1.0.0")
        tarball = _pack_archive(tmp_path, tmp_path / "apm.yml", pkg, MagicMock(), verbose=False)
        members = _list_tar_members(tarball.read_bytes())
        assert not any("._" in m for m in members)

    def test_missing_dot_apm_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\ndescription: x\nauthor: a\n",
            encoding="utf-8",
        )
        pkg = APMPackage(name="demo", version="1.0.0")
        with pytest.raises(ClickException, match="requires a flat APM package"):
            _pack_archive(tmp_path, tmp_path / "apm.yml", pkg, MagicMock(), verbose=False)


class TestResolvePackageId:
    """Unit tests for ``_resolve_package_id`` parsing.

    ``--package`` is enforced as required by Click before the command body
    runs, so ``_resolve_package_id`` only needs to parse a guaranteed
    non-None string value into (owner, repo).
    """

    def test_bare_owner_repo(self) -> None:
        assert _resolve_package_id("acme/my-skill") == ("acme", "my-skill")

    def test_github_https_url_stripped(self) -> None:
        assert _resolve_package_id("https://github.com/acme/my-skill") == ("acme", "my-skill")

    def test_malformed_value_raises(self) -> None:
        with pytest.raises(UsageError, match="owner/repo"):
            _resolve_package_id("not-a-valid-id")
