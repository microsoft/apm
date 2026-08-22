"""Architecture guardrails for the bundle-format authority."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


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


def test_agent_plugin_guard_requires_admissibility_before_legacy_fallback(
    tmp_path: Path,
) -> None:
    """The checker must reject removal of the present-manifest admissibility gate."""
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

    loader = sandbox / "src/apm_cli/agent_plugins/loader.py"
    loader.write_text(
        loader.read_text(encoding="utf-8").replace(
            "read_json_document(manifest_path, reject_duplicate_schema=True)",
            "read_json_document(manifest_path)",
        ),
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
        "[x] Agent Plugin loader must own admissibility, detection, loading, and manifest authority"
    )


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "message"),
    [
        (
            "src/apm_cli/bundle/formats.py",
            "PREFERRED_PLUGIN_FORMAT = BundleFormat.CLAUDE_PLUGIN",
            "PREFERRED_PLUGIN_FORMAT = BundleFormat.AGENT_PLUGIN",
            "Agent Plugin preferred-default flip is reserved for T10 after G3",
        ),
        (
            "src/apm_cli/bundle/formats.py",
            '"plugin": BundleFormat.CLAUDE_PLUGIN',
            '"plugin": BundleFormat.AGENT_PLUGIN',
            "plugin format token must remain Claude-compatible for apm-action@v1",
        ),
        (
            "src/apm_cli/commands/plugin/init.py",
            '    "--format",',
            '    "--plugin",',
            "Portable Agent Plugins must use --format agent-plugin, not --plugin",
        ),
        (
            "src/apm_cli/bundle/formats.py",
            "if len(selections) > 1:",
            "if len(selections) > 2:",
            "Bundle selectors and no-flag behavior must route through the canonical format seam",
        ),
        (
            "src/apm_cli/commands/init.py",
            "plugin = load_agent_plugin(staged_root)",
            "plugin = None",
            "Plugin scaffolding must share the preferred-format seam and canonical reload",
        ),
        (
            "src/apm_cli/bundle/reproducible_archive.py",
            "shutil.copyfileobj(source, member)",
            "member.write(path.read_bytes())",
            "Reproducible archives must stream file payloads without full-file buffering",
        ),
        (
            "src/apm_cli/bundle/agent_plugin_exporter.py",
            "    _require_portable_agent_plugin(dropped_surfaces)\n",
            "    # Portable-surface admission bypassed.\n",
            "Agent Plugin portable-surface admission must fail before output projection",
        ),
    ],
)
def test_producer_projection_guard_kills_contract_mutations(
    tmp_path: Path,
    relative_path: str,
    old: str,
    new: str,
    message: str,
) -> None:
    """The static gate must kill default, init, omission, and streaming mutants."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    paths = {
        "scripts/check_bundle_format_authority.sh",
        "src/apm_cli/bundle/formats.py",
        relative_path,
    }
    for relative in paths:
        source = root / relative
        destination = sandbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    mutation_path = sandbox / relative_path
    source = mutation_path.read_text(encoding="utf-8")
    assert old in source
    mutation_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    result = subprocess.run(
        ("bash", str(sandbox / "scripts/check_bundle_format_authority.sh"), str(sandbox)),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert message in result.stdout
