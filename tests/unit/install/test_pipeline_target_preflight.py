"""Pipeline rejects an undeployable manifest target before any write."""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _run_phase: basic contracts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: github host skipped
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: timeout sentinel continues
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: non-auth failure continues
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: ADO auth failure raises AuthenticationError
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: generic host prunes isolation env keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _preflight_auth_check: verbose mode calls logger trace
# ---------------------------------------------------------------------------


# Trace may or may not fire depending on host type; main assertion is no exception.


# ---------------------------------------------------------------------------
# _preflight_auth_check: deduplication via seen set
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline: early returns
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_install_pipeline: exception re-raise contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["intellij", "copilot-app"])
def test_pipeline_rejects_undeployable_manifest_target_before_any_write(
    token: str, tmp_path, monkeypatch
) -> None:
    """Direct pipeline callers get the CLI promise: exit 2 before writing anything."""
    from apm_cli.install.pipeline import run_install_pipeline
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.utils.yaml_io import dump_yaml

    manifest = tmp_path / "apm.yml"
    dump_yaml({"name": "p", "version": "1.0.0", "target": token}, manifest)
    pkg = APMPackage.from_apm_yml(manifest)
    monkeypatch.chdir(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    with pytest.raises(SystemExit) as excinfo:
        run_install_pipeline(pkg)

    assert excinfo.value.code == 2
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
