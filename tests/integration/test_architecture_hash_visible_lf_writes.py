"""Architecture guardrails for generated files inside raw-hashed package trees."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_hash_visible_writes_route_through_lf_helpers() -> None:
    """The boundary script must enforce every hash-visible writer."""
    root = Path(__file__).parents[2]
    result = subprocess.run(
        (sys.executable, "scripts/check_hash_visible_lf_writes.py"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text(encoding="utf-8")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "AC34: hash-visible generated files use canonical LF writers" in guard
    assert "check_hash_visible_lf_writes.py" in guard


def test_hash_visible_write_guard_rejects_platform_native_bypass(
    tmp_path: Path,
) -> None:
    """The guard rejects restoring a direct platform-native text write."""
    root = Path(__file__).parents[2]
    sandbox = tmp_path / "repo"
    relative_paths = (
        "scripts/check_hash_visible_lf_writes.py",
        "src/apm_cli/deps/plugin_parser.py",
        "src/apm_cli/utils/yaml_io.py",
    )
    for relative_path in relative_paths:
        source = root / relative_path
        destination = sandbox / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    parser_path = sandbox / "src/apm_cli/deps/plugin_parser.py"
    source = parser_path.read_text(encoding="utf-8")
    expected = "write_text_lf(apm_yml_path, apm_yml_content)"
    assert expected in source
    parser_path.write_text(
        source.replace(
            expected,
            'apm_yml_path.write_text(apm_yml_content, encoding="utf-8")',
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        (sys.executable, "scripts/check_hash_visible_lf_writes.py", str(sandbox)),
        cwd=sandbox,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "synthesize_apm_yml_from_plugin must call write_text_lf exactly once" in result.stdout
    assert "bypasses canonical LF writer via write_text" in result.stdout
