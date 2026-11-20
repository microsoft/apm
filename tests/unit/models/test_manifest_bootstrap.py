"""Golden bytes for the minimal apm.yml bootstrap writer (#3879).

Moving the writer below the commands layer must not change what it writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.manifest_bootstrap import write_minimal_manifest

_SCAFFOLD = (
    "name: p\n"
    "version: 1.0.0\n"
    "description: d\n"
    "{author}"
    "# Which agent platforms to deploy to (uncomment to pin):\n"
    "# targets:\n"
    "#   - copilot\n"
    "#   - claude\n"
    "\n"
    "dependencies:\n"
    "  apm: []\n"
    "  mcp: []\n"
    "includes: auto\n"
    "{dev}"
    "scripts: {{}}\n"
)


@pytest.mark.parametrize(
    ("config", "plugin", "expected"),
    [
        pytest.param(
            {"name": "p", "version": "1.0.0", "description": "d"},
            False,
            _SCAFFOLD.format(author="", dev=""),
            id="install-bootstrap",
        ),
        pytest.param(
            {"name": "p", "version": "1.0.0", "description": "d", "author": "a"},
            True,
            _SCAFFOLD.format(author="author: a\n", dev="devDependencies:\n  apm: []\n").replace(
                "\ndependencies:\n",
                "\n# SPDX license for this package (set before publishing), for example:\n"
                "# license: MIT\n\ndependencies:\n",
            ),
            id="plugin-init",
        ),
    ],
)
def test_minimal_manifest_bytes(
    tmp_path: Path, config: dict[str, str], plugin: bool, expected: str
) -> None:
    target = tmp_path / "apm.yml"
    write_minimal_manifest(dict(config), plugin=plugin, target_path=target)
    assert target.read_text(encoding="utf-8") == expected
