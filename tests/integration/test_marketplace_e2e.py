"""End-to-end binary-level tests for the `apm marketplace` CLI surface.

Covers gap G3.5 -- the marketplace flow (`marketplace add` / `list` /
`remove`, then `install plugin@marketplace`) had no binary-level coverage
even though the underlying modules (registry, client, resolver) are
unit-tested.

Tests 1 and 3 seed `~/.apm/marketplaces.json` directly and exercise the
config-only commands (`list`, `remove`) that do not require network
access. Test 2 exercises the `add` command's input-validation path,
which also runs without network.

The full `add -> install plugin@marketplace -> deploy` flow requires a
public marketplace.json hosted on GitHub plus a token; that scenario is
left intentionally as a follow-up since no public marketplace fixture is
maintained alongside this repository today (see `apm-sample-package`,
which is a plain APM package, not a marketplace).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SAMPLE_MARKETPLACE_NAME = "test-mkt"


@pytest.fixture
def apm_command():
    """Resolve the apm CLI executable (PATH first, then local venv)."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def fake_home(tmp_path):
    """Isolated HOME so registry writes never touch the real user config."""
    home_dir = tmp_path / "fakehome"
    home_dir.mkdir()
    return home_dir


def _env_with_home(fake_home):
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    if sys.platform == "win32":
        env["USERPROFILE"] = str(fake_home)
    return env


def _run_apm(apm_command, args, fake_home, cwd=None, timeout=60):
    return subprocess.run(
        [apm_command] + args,  # noqa: RUF005
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env_with_home(fake_home),
    )


def _seed_marketplace(
    fake_home, name=SAMPLE_MARKETPLACE_NAME, owner="acme-org", repo="plugin-marketplace"
):
    """Write a valid marketplaces.json directly, bypassing the network call
    that `apm marketplace add` performs."""
    apm_dir = fake_home / ".apm"
    apm_dir.mkdir(parents=True, exist_ok=True)
    payload = {"marketplaces": [{"name": name, "owner": owner, "repo": repo}]}
    (apm_dir / "marketplaces.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_marketplace_list_shows_seeded_entry(apm_command, fake_home):
    """`apm marketplace list` surfaces entries persisted in the registry."""
    _seed_marketplace(fake_home)

    result = _run_apm(apm_command, ["marketplace", "list"], fake_home)

    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    combined = result.stdout + result.stderr
    assert SAMPLE_MARKETPLACE_NAME in combined
    assert "acme-org/plugin-marketplace" in combined


def test_marketplace_add_rejects_invalid_format(apm_command, fake_home):
    """`apm marketplace add` validates OWNER/REPO format without hitting the
    network (validation happens before the GitHub fetch)."""
    result = _run_apm(apm_command, ["marketplace", "add", "not-a-valid-repo"], fake_home)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Invalid format" in combined or "OWNER/REPO" in combined

    # Registry file must NOT have been created/populated
    registry_file = fake_home / ".apm" / "marketplaces.json"
    if registry_file.exists():
        data = json.loads(registry_file.read_text(encoding="utf-8"))
        assert data.get("marketplaces", []) == []


def test_marketplace_remove_clears_entry(apm_command, fake_home):
    """`apm marketplace remove --yes` deletes the entry from the registry."""
    _seed_marketplace(fake_home)

    remove_result = _run_apm(
        apm_command,
        ["marketplace", "remove", SAMPLE_MARKETPLACE_NAME, "--yes"],
        fake_home,
    )
    assert remove_result.returncode == 0, (
        f"stdout={remove_result.stdout!r}\nstderr={remove_result.stderr!r}"
    )

    list_result = _run_apm(apm_command, ["marketplace", "list"], fake_home)
    assert list_result.returncode == 0
    combined = list_result.stdout + list_result.stderr
    assert SAMPLE_MARKETPLACE_NAME not in combined

    registry_file = fake_home / ".apm" / "marketplaces.json"
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    assert data.get("marketplaces", []) == []


@pytest.mark.skip(
    reason="Full add->install->deploy flow needs a public marketplace.json "
    "fixture on GitHub; no canonical public marketplace is maintained "
    "alongside this repo. See gap G3.5 follow-up."
)
def test_marketplace_install_resolves_and_deploys():
    """Placeholder for the full end-to-end install path."""
    pass
