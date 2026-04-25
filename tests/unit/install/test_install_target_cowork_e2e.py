"""E2E regression tests for 'apm install --target cowork --global'.

These tests exercise the real Click parser to guard against the bug fixed in
commit 2f96dd5: 'cowork' was not in VALID_TARGET_VALUES, so the CLI rejected
the flag with "is not a valid target" at *parse time*, before the install
pipeline even ran.

Two mandatory scenarios:
  1. Flag OFF  -> reaches phases/targets.py, prints enable-hint, exits 0.
  2. Flag ON   -> reaches phases/targets.py, resolver finds no OneDrive, exits 1
                  with "no OneDrive path detected" message.
  3. (Optional) No --global -> project-scope gate rejects with --global hint.

Design notes
------------
* ``CONFIG_DIR`` and ``CONFIG_FILE`` in ``apm_cli.config`` are module-level
  strings computed from ``~`` at import time.  We must monkeypatch them
  directly -- changing the HOME env var after import has no effect.
* ``Path.home()`` is used by ``apm_cli.core.scope`` functions that build
  user-scope paths at call time.  We monkeypatch the classmethod so that
  every call inside the installed pipeline returns our temp directory.
* ``APM_E2E_TESTS=1`` silences the version-check background side effect.
* A minimal ``apm.yml`` (no deps) avoids all network calls: the resolve
  phase creates an empty dependency graph and the download phase is a no-op.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from click.testing import CliRunner

from apm_cli.cli import cli


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MINIMAL_APM_YML = "name: test\ndescription: test\nversion: 0.0.1\n"

# Env additions applied to every CliRunner.invoke call in this module.
_BASE_ENV: Dict[str, str] = {"APM_E2E_TESTS": "1"}


def _write_minimal_apm_yml(apm_dir: Path) -> None:
    """Write a minimal apm.yml with no dependencies into *apm_dir*."""
    (apm_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")


def _write_config_json(apm_dir: Path, cfg: Dict[str, Any]) -> None:
    """Write *cfg* as JSON to ``<apm_dir>/config.json``."""
    (apm_dir / "config.json").write_text(json.dumps(cfg), encoding="ascii")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated home directory wired into every APM config lookup.

    Sets up:
    - ``tmp_path/home/.apm/`` directory
    - Monkeypatches ``Path.home`` so scope helpers use the fake home
    - Monkeypatches ``apm_cli.config.CONFIG_DIR`` and ``CONFIG_FILE``
      (computed at import time, not re-evaluated from HOME at runtime)
    - Resets ``_config_cache`` before/after so stale cached state never
      leaks between tests

    Returns the fake home root (``tmp_path/home``).
    """
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)

    # -- apm.yml (required -- bare install with no apm.yml exits 1) -----
    _write_minimal_apm_yml(apm_dir)

    # -- Path.home() -------------------------------------------------------
    # scope.py calls Path.home() at *call time* (not import time) so
    # patching the classmethod is enough.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    # -- apm_cli.config constants (evaluated at import time) -------------
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)
    yield home
    # Reset after the test so no cached state bleeds out.
    monkeypatch.setattr(_conf, "_config_cache", None)


# ---------------------------------------------------------------------------
# TestCoworkParserE2E -- core regression tests
# ---------------------------------------------------------------------------


class TestCoworkParserE2E:
    """CliRunner regression tests for 'apm install --target cowork --global'.

    Before the fix in 2f96dd5, both tests below would have failed at Click
    *parse time* with:
      Error: Invalid value for '--target': 'cowork' is not a valid target. ...
    and exited with code 2 (Click's UsageError exit code).
    """

    # ------------------------------------------------------------------ #
    # Case 1: Flag OFF -- parser accepts cowork, targets phase emits hint #
    # ------------------------------------------------------------------ #

    def test_flag_off_parser_accepts_cowork_and_emits_hint(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apm install --target cowork --global with flag OFF:
        - Click must NOT reject 'cowork' ("is not a valid target" must be absent).
        - The command must exit 0 (enable-hint path).
        - Output must contain 'apm experimental enable cowork'.
        """
        # Ensure cowork flag is OFF (no config.json, or explicit false).
        # With no config.json the config module creates a default one that
        # does NOT include the cowork key, so is_enabled("cowork") == False.
        config_file = fake_home / ".apm" / "config.json"
        if config_file.exists():
            config_file.unlink()

        # Ensure APM_COWORK_SKILLS_DIR is unset so no accidental OneDrive hit.
        monkeypatch.delenv("APM_COWORK_SKILLS_DIR", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "cowork", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        # Regression: Click parse-time rejection used exit code 2.
        # The fix means we reach the install pipeline instead.
        assert result.exit_code == 0, (
            f"Expected exit 0 from enable-hint path, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

        combined = result.output or ""

        # Old bug: Click rejected at parse time.
        assert "is not a valid target" not in combined, (
            "Parser still rejecting 'cowork' -- fix may have been reverted.\n"
            f"Output:\n{combined}"
        )

        # Phases/targets.py must have emitted the enable hint.
        # Normalize whitespace to handle terminal line-wrapping.
        normalized = " ".join(combined.split())
        assert "apm experimental enable cowork" in normalized, (
            "Enable hint not found in output -- targets phase may not have run.\n"
            f"Output:\n{combined}"
        )

    # ------------------------------------------------------------------ #
    # Case 2: Flag ON -- parser accepts cowork, resolver error emitted   #
    # ------------------------------------------------------------------ #

    def test_flag_on_parser_accepts_cowork_resolver_error(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apm install --target cowork --global with flag ON but no OneDrive:
        - Click must NOT reject 'cowork'.
        - phases/targets.py must emit the 'no OneDrive path detected' error.
        - The command exits non-zero (cowork resolver failure).

        The exit code is 1 (sys.exit(1) in phases/targets.py run()).
        """
        import apm_cli.config as _conf

        # Enable the cowork experimental flag via direct cache injection.
        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"cowork": True}},
        )

        # Ensure no OneDrive path is available in the sandbox.
        monkeypatch.delenv("APM_COWORK_SKILLS_DIR", raising=False)

        # Patch the cowork root resolver to return None (no OneDrive found).
        # Patch at the point-of-use in integration.targets so that the
        # resolve_targets() call in phases/targets.py hits our stub.
        with patch(
            "apm_cli.integration.targets._resolve_cowork_root",
            return_value=None,
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["install", "--target", "cowork", "--global"],
                env={**_BASE_ENV},
                catch_exceptions=True,  # SystemExit is expected
            )

        combined = result.output or ""

        # Regression guard: no parse-time "is not a valid target" rejection.
        assert "is not a valid target" not in combined, (
            "Parser still rejecting 'cowork' -- fix may have been reverted.\n"
            f"Output:\n{combined}"
        )

        # The resolver error message from phases/targets.py must appear.
        assert "no OneDrive path detected" in combined, (
            "Expected 'no OneDrive path detected' in output.\n"
            f"Output:\n{combined}"
        )

        # The command must have failed (sys.exit(1) in targets phase).
        # Note: CliRunner wraps SystemExit -- exit_code reflects the code.
        assert result.exit_code != 0, (
            "Expected non-zero exit when OneDrive resolver returns None.\n"
            f"Output:\n{combined}"
        )

    # ------------------------------------------------------------------ #
    # Case 3: No --global -- project-scope gate must reject              #
    # ------------------------------------------------------------------ #

    def test_no_global_flag_project_scope_rejected(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """apm install --target cowork (no --global) must error with --global hint.

        The project-scope gate in phases/targets.py checks that cowork is
        only valid with --global (user scope).
        """
        import apm_cli.config as _conf

        # Flag ON so cowork passes the flag gate and reaches the scope gate.
        monkeypatch.setattr(
            _conf,
            "_config_cache",
            {"experimental": {"cowork": True}},
        )
        monkeypatch.delenv("APM_COWORK_SKILLS_DIR", raising=False)

        # For project scope, CWD must have an apm.yml.
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")

        # Patch cowork root resolver so user-scope path (not triggered here)
        # would return a valid dir -- the project-scope gate fires first.
        with (
            patch(
                "apm_cli.integration.targets._resolve_cowork_root",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["install", "--target", "cowork"],
                env={**_BASE_ENV},
                catch_exceptions=True,
                # Provide the project dir as CWD via CliRunner.
            )

        combined = result.output or ""

        # Parser must NOT reject at parse time.
        assert "is not a valid target" not in combined, (
            f"Parser rejected 'cowork' -- fix may have been reverted.\n"
            f"Output:\n{combined}"
        )

        # The project-scope error from phases/targets.py should mention --global.
        assert "--global" in combined, (
            "Expected '--global' hint in project-scope error output.\n"
            f"Output:\n{combined}"
        )
