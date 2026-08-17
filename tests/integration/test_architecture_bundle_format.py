"""Architecture guardrails for the bundle-format authority."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_bundle_format_guard_rejects_parallel_authority(tmp_path: Path) -> None:
    """The boundary checker must reject a second format resolver."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    script = sandbox / "scripts/check_bundle_format_authority.sh"
    owner = sandbox / "src/apm_cli/bundle/formats.py"
    duplicate = sandbox / "src/apm_cli/bundle/duplicate.py"
    script.parent.mkdir(parents=True)
    owner.parent.mkdir(parents=True)
    shutil.copy2(root / "scripts/check_bundle_format_authority.sh", script)
    shutil.copy2(root / "src/apm_cli/bundle/formats.py", owner)
    duplicate.write_text(
        "def resolve_bundle_format():\n    return 'parallel'\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", str(script), str(sandbox)),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == (
        "[x] Bundle format authority must live in src/apm_cli/bundle/formats.py"
    )


def test_agent_plugin_guard_rejects_parallel_contract_loader(tmp_path: Path) -> None:
    """The boundary checker must reject a second Agent Plugin interpreter."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    paths = (
        "scripts/check_bundle_format_authority.sh",
        "src/apm_cli/bundle/formats.py",
        "src/apm_cli/agent_plugins/loader.py",
        "src/apm_cli/models/validation.py",
        "src/apm_cli/models/format_detection.py",
        "src/apm_cli/deps/plugin_parser.py",
    )
    for relative in paths:
        source = root / relative
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    duplicate = sandbox / "src/apm_cli/duplicate_agent_plugin.py"
    duplicate.write_text(
        "def load_agent_plugin(package_root):\n    return package_root\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", str(sandbox / "scripts/check_bundle_format_authority.sh"), str(sandbox)),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == (
        "[x] Agent Plugin interpretation must live in src/apm_cli/agent_plugins/loader.py"
    )


def test_agent_plugin_guard_rejects_parallel_in_package_loader(tmp_path: Path) -> None:
    """The checker must not exempt parallel owners inside agent_plugins."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    paths = (
        "scripts/check_bundle_format_authority.sh",
        "src/apm_cli/bundle/formats.py",
        "src/apm_cli/agent_plugins/loader.py",
        "src/apm_cli/agent_plugins/ir.py",
        "src/apm_cli/models/validation.py",
        "src/apm_cli/models/format_detection.py",
        "src/apm_cli/deps/plugin_parser.py",
    )
    for relative in paths:
        source = root / relative
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    duplicate = sandbox / "src/apm_cli/agent_plugins/parallel.py"
    duplicate.write_text(
        "def load_agent_plugin(package_root):\n    return package_root\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ("bash", str(sandbox / "scripts/check_bundle_format_authority.sh"), str(sandbox)),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == (
        "[x] Agent Plugin interpretation must live in src/apm_cli/agent_plugins/loader.py"
    )
