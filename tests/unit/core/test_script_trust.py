"""Unit tests for the script trust gate (script_trust.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import yaml

from apm_cli.core.script_trust import (
    is_project_scripts_trusted,
    script_file_fingerprint,
    trust_project_scripts,
    untrust_project_scripts,
)


def _write_apm_yml(path: Path, data: dict) -> Path:
    """Write YAML test data to apm.yml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
    return path


class TestScriptFileFingerprint:
    def test_returns_sha256_of_lifecycle_subtree(self, tmp_path: Path) -> None:
        lifecycle = {"post-install": [{"type": "command", "bash": "echo hi"}]}
        f = _write_apm_yml(tmp_path / "apm.yml", {"name": "pkg", "lifecycle": lifecycle})
        expected = hashlib.sha256(
            json.dumps(lifecycle, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert script_file_fingerprint(f) == expected

    def test_returns_none_for_missing_lifecycle(self, tmp_path: Path) -> None:
        f = _write_apm_yml(tmp_path / "apm.yml", {"name": "pkg"})
        assert script_file_fingerprint(f) is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert script_file_fingerprint(tmp_path / "missing.yml") is None


class TestIsProjectScriptsTrusted:
    def test_returns_false_when_no_trust_store(self, tmp_path: Path) -> None:
        script_file = _write_apm_yml(tmp_path / "apm.yml", {"lifecycle": {"post-install": []}})
        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert not is_project_scripts_trusted(script_file)

    def test_returns_true_when_fingerprint_matches(self, tmp_path: Path) -> None:
        script_file = _write_apm_yml(tmp_path / "apm.yml", {"lifecycle": {"post-install": []}})
        fingerprint = script_file_fingerprint(script_file)
        assert fingerprint is not None
        trust_store = tmp_path / "scripts-trust.json"
        trust_store.write_text(
            json.dumps({"version": 1, "projects": {str(script_file.resolve()): fingerprint}}),
            encoding="utf-8",
        )
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            assert is_project_scripts_trusted(script_file)


class TestTrustProjectScripts:
    def test_records_fingerprint_and_returns_it(self, tmp_path: Path) -> None:
        script_file = _write_apm_yml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo hi"}]}},
        )
        expected_fp = script_file_fingerprint(script_file)
        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            result = trust_project_scripts(script_file)
        assert result == expected_fp
        stored = json.loads(trust_store.read_text(encoding="utf-8"))
        assert stored["projects"][str(script_file.resolve())] == expected_fp

    def test_dependencies_change_does_not_revoke_trust(self, tmp_path: Path) -> None:
        script_file = _write_apm_yml(
            tmp_path / "apm.yml",
            {
                "dependencies": {"a": {"git": "https://example.com/a.git"}},
                "lifecycle": {"post-install": [{"type": "command", "bash": "echo hi"}]},
            },
        )
        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            trust_project_scripts(script_file)
            assert is_project_scripts_trusted(script_file)
            _write_apm_yml(
                script_file,
                {
                    "dependencies": {"b": {"git": "https://example.com/b.git"}},
                    "lifecycle": {"post-install": [{"type": "command", "bash": "echo hi"}]},
                },
            )
            assert is_project_scripts_trusted(script_file)
            _write_apm_yml(
                script_file,
                {"lifecycle": {"post-install": [{"type": "command", "bash": "echo bye"}]}},
            )
            assert not is_project_scripts_trusted(script_file)


class TestUntrustProjectScripts:
    def test_removes_trust_record_and_returns_true(self, tmp_path: Path) -> None:
        script_file = _write_apm_yml(tmp_path / "apm.yml", {"lifecycle": {"post-install": []}})
        trust_store = tmp_path / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            trust_project_scripts(script_file)
            result = untrust_project_scripts(script_file)
            assert result is True
            assert not is_project_scripts_trusted(script_file)
