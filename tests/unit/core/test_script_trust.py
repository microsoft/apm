"""Unit tests for the script trust gate (script_trust.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from apm_cli.core.script_trust import (
    is_project_scripts_trusted,
    script_file_fingerprint,
    trust_project_scripts,
    untrust_project_scripts,
)

# -- script_file_fingerprint -----------------------------------------------


class TestScriptFileFingerprint:
    def test_returns_sha256_hex_digest(self, tmp_path: Path) -> None:
        content = b'{"version": 1, "scripts": {}}'
        f = tmp_path / "scripts.json"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert script_file_fingerprint(f) == expected

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert script_file_fingerprint(tmp_path / "nonexistent.json") is None

    def test_returns_none_for_unreadable_file(self, tmp_path: Path) -> None:
        f = tmp_path / "scripts.json"
        f.write_bytes(b"{}")
        with patch("pathlib.Path.read_bytes", side_effect=OSError("permission denied")):
            assert script_file_fingerprint(f) is None


# -- is_project_scripts_trusted --------------------------------------------


class TestIsProjectScriptsTrusted:
    def test_returns_false_when_no_trust_store(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        script_file.write_text('{"version": 1, "scripts": {}}')

        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert not is_project_scripts_trusted(script_file)

    def test_returns_false_when_fingerprint_mismatches(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        content = b'{"version": 1, "scripts": {}}'
        script_file.write_bytes(content)

        trust_store = tmp_path / "scripts-trust.json"
        trust_store.write_text(
            json.dumps({"version": 1, "projects": {str(script_file.resolve()): "badhash"}})
        )
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert not is_project_scripts_trusted(script_file)

    def test_returns_true_when_fingerprint_matches(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        content = b'{"version": 1, "scripts": {}}'
        script_file.write_bytes(content)
        fp = hashlib.sha256(content).hexdigest()

        trust_store = tmp_path / "scripts-trust.json"
        trust_store.write_text(
            json.dumps({"version": 1, "projects": {str(script_file.resolve()): fp}})
        )
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert is_project_scripts_trusted(script_file)

    def test_returns_false_for_missing_script_file(self, tmp_path: Path) -> None:
        missing = tmp_path / ".apm" / "scripts.json"
        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert not is_project_scripts_trusted(missing)

    def test_returns_false_when_trust_store_is_malformed(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        script_file.write_text('{"version": 1, "scripts": {}}')

        trust_store = tmp_path / "scripts-trust.json"
        trust_store.write_text("not valid json{{{")

        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert not is_project_scripts_trusted(script_file)


# -- trust_project_scripts -------------------------------------------------


class TestTrustProjectScripts:
    def test_records_fingerprint_and_returns_it(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        content = b'{"version": 1, "scripts": {}}'
        script_file.write_bytes(content)
        expected_fp = hashlib.sha256(content).hexdigest()

        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            result = trust_project_scripts(script_file)

        assert result == expected_fp
        stored = json.loads(trust_store.read_text())
        assert stored["projects"][str(script_file.resolve())] == expected_fp

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / ".apm" / "scripts.json"
        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert trust_project_scripts(missing) is None

    def test_updating_file_content_changes_stored_fingerprint(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        script_file.write_bytes(b'{"version": 1, "scripts": {}}')

        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            fp1 = trust_project_scripts(script_file)
            script_file.write_bytes(b'{"version": 1, "scripts": {"post-install": []}}')
            fp2 = trust_project_scripts(script_file)

        assert fp1 != fp2


# -- untrust_project_scripts -----------------------------------------------


class TestUntrustProjectScripts:
    def test_removes_trust_record_and_returns_true(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        script_file.write_bytes(b'{"version": 1, "scripts": {}}')

        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            trust_project_scripts(script_file)
            result = untrust_project_scripts(script_file)

        assert result is True
        stored = json.loads(trust_store.read_text())
        assert str(script_file.resolve()) not in stored["projects"]

    def test_returns_false_when_not_trusted(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        script_file.write_text('{"version": 1, "scripts": {}}')

        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            result = untrust_project_scripts(script_file)

        assert result is False

    def test_untrust_makes_is_trusted_return_false(self, tmp_path: Path) -> None:
        script_file = tmp_path / ".apm" / "scripts.json"
        script_file.parent.mkdir(parents=True)
        script_file.write_bytes(b'{"version": 1, "scripts": {}}')

        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            trust_project_scripts(script_file)
            assert is_project_scripts_trusted(script_file)
            untrust_project_scripts(script_file)
            assert not is_project_scripts_trusted(script_file)
