"""Regenerate the pre-0.36 upgrade fixture with a released APM wheel (#4398).

Usage (from the repository root; any released version, e.g. 0.35.1 or 0.36.1):

    uv venv /tmp/apm-<version> --python 3.12
    uv pip install --python /tmp/apm-<version>/bin/python apm-cli==<version>
    .venv/bin/python tests/fixtures/pre036_upgrade/generate.py \
        --apm /tmp/apm-<version>/bin/apm --out tests/fixtures/pre036_upgrade/v<version>

A released binary from the GitHub release works too (``gh release download
v<version> -p 'apm-<os>-<arch>.tar.gz*'``, checked against its ``.sha256``);
pass ``--provenance`` to record how it was obtained. ``--case copilot-kiro-mcp``
produces the Copilot + Kiro project with a self-defined MCP server.

The released binary installs one consumer project with one local ``path:``
dependency in a temp HOME without network access. The package carries
primitives whose 0.37 rendering differs from the 0.35 one (a Cursor rule with
``applyTo: "**"`` and a Codex agent with a relative link) next to ones
that render identically. ``apm.yml``, ``apm.lock.yaml``, every deployed file
(outside ``apm_modules/``) and the package are copied verbatim, and
``FIXTURE.json`` records the producing ``apm --version``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment  # noqa: E402

PACKAGE_FILES = {
    "apm.yml": "name: legacy-pkg\nversion: 1.0.0\ndescription: Pre-0.36 upgrade fixture\n",
    ".apm/instructions/everywhere.instructions.md": (
        '---\napplyTo: "**"\ndescription: Everywhere\n---\n# Everywhere\nBe kind.\n'
    ),
    ".apm/instructions/python.instructions.md": (
        '---\napplyTo: "**/*.py"\ndescription: Python style\n---\n# Python\nUse type hints.\n'
    ),
    ".apm/agents/editor.agent.md": (
        "---\nname: editor\ndescription: Edits code\n"
        'tools: ["read", "edit"]\nmodel: gpt-5\n---\nYou edit code.\n'
        "Follow [the style](../instructions/python.instructions.md).\n"
    ),
    ".apm/agents/reviewer.agent.md": (
        "---\nname: reviewer\ndescription: Reviews code\n---\nYou review code.\n"
    ),
    ".apm/skills/legacy-skill/SKILL.md": (
        "---\nname: legacy-skill\ndescription: Legacy skill\n---\n# Legacy skill\nDo it.\n"
    ),
}
# Copilot + Kiro project whose package declares a self-defined MCP server, the
# most common real upgrade: 0.36.1 writes .github/mcp.json, 0.37 writes the root
# .mcp.json (#3866).
COPILOT_KIRO_PACKAGE_FILES = {
    "apm.yml": (
        "name: mcp-pkg\nversion: 1.0.0\ndescription: Copilot and Kiro upgrade fixture\n"
        "dependencies:\n  mcp:\n  - name: notes\n    registry: false\n"
        "    transport: stdio\n    command: node\n    args:\n    - server.js\n"
    ),
    ".apm/instructions/python.instructions.md": (
        '---\napplyTo: "**/*.py"\ndescription: Python style\n---\n# Python\nUse type hints.\n'
    ),
    ".apm/agents/helper.agent.md": ("---\nname: helper\ndescription: Helps\n---\nYou help.\n"),
    ".apm/skills/notes-skill/SKILL.md": (
        "---\nname: notes-skill\ndescription: Notes skill\n---\n# Notes\nTake notes.\n"
    ),
}
COPILOT_KIRO_MANIFEST = (
    "name: copilot-kiro-project\nversion: 1.0.0\n"
    "targets:\n- copilot\n- kiro\n"
    "dependencies:\n  apm:\n  - path: ../pkg\n"
)
MANIFEST = (
    "name: legacy-project\nversion: 1.0.0\n"
    "targets:\n- claude\n- cursor\n- codex\n"
    "dependencies:\n  apm:\n  - path: ../pkg\n"
)
TARGET_DIRS = (".claude", ".cursor", ".codex")
CASES = {
    "legacy": (PACKAGE_FILES, MANIFEST, TARGET_DIRS),
    "copilot-kiro-mcp": (COPILOT_KIRO_PACKAGE_FILES, COPILOT_KIRO_MANIFEST, (".github", ".kiro")),
}
SKIP = {"apm_modules", ".apm-local"}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")


def _copy_project(source: Path, out: Path) -> None:
    for item in sorted(source.iterdir()):
        if item.name in SKIP:
            continue
        target = out / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apm", required=True, type=Path, help="released apm executable")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--case", choices=sorted(CASES), default="legacy")
    parser.add_argument(
        "--provenance", help="how the released apm was obtained (recorded in FIXTURE.json)"
    )
    args = parser.parse_args()
    package_files, manifest_text, target_dirs = CASES[args.case]

    with tempfile.TemporaryDirectory() as scratch:
        world = IsolatedApmEnvironment.create(Path(scratch) / "world", base_env=dict(os.environ))
        env = world.subprocess_env()
        version_text = subprocess.run(
            [str(args.apm), "--version"], check=True, capture_output=True, text=True, env=env
        ).stdout.strip()
        match = re.search(r"version (\d+\.\d+\.\d+)", version_text)
        if match is None:
            raise SystemExit(f"cannot read the APM version from: {version_text!r}")
        root = world.work_root / "fixture"
        _write(root / "pkg", package_files)
        project = root / "project"
        project.mkdir(parents=True)
        (project / "apm.yml").write_text(manifest_text, encoding="ascii")
        for name in target_dirs:
            (project / name).mkdir()
        subprocess.run([str(args.apm), "install", "--no-policy"], check=True, cwd=project, env=env)
        if args.out.exists():
            shutil.rmtree(args.out)
        (args.out / "project").mkdir(parents=True)
        _copy_project(project, args.out / "project")
        shutil.copytree(root / "pkg", args.out / "pkg")
        manifest = {
            "apm_version": match.group(1),
            "apm_version_output": version_text,
            "case": args.case,
        }
        if args.provenance:
            manifest["provenance"] = args.provenance
        (args.out / "FIXTURE.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
