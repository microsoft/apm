"""Regenerate the T2 upgrade fixtures (manifest target spellings x APM version).

Usage (from the repository root, with a checkout of the producing version):

    .venv/bin/python tests/fixtures/upgrade_0361/t2/generate.py \
        --apm-src <checkout>/src --version 0.35.1 --out tests/fixtures/upgrade_0361/t2/v0.35.1

Each case is a consumer project with one local ``path:`` dependency and one
self-defined MCP server, installed once by the producing version in a temp
HOME without network access. The resulting ``apm.yml``, ``apm.lock.yaml``
and every deployed file (outside ``apm_modules/``) are copied verbatim.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment  # noqa: E402

MCP_BLOCK = (
    "  mcp:\n"
    "  - name: srv\n"
    "    registry: false\n"
    "    transport: http\n"
    "    url: https://mcp.example.com/mcp\n"
)

CASES: dict[str, str] = {
    "singular": "target: claude\n",
    "singular-csv": "target: claude,copilot\n",
    "singular-alias": "target: vscode\n",
    "singular-all": "target: all\n",
    "plural": "targets:\n- claude\n- copilot\n",
    "plural-alias": "targets:\n- vscode\n",
    "plural-all": "targets:\n- all\n",
}

# Projects whose declaration folds to auto-detect get one detection signal.
AUTO_DETECT_DIRS = {"plural-all": ".claude"}


def write_package(root: Path) -> None:
    files = {
        "apm.yml": "name: pkg\nversion: 1.0.0\ndescription: Upgrade fixture package\n",
        ".apm/skills/pkg-skill/SKILL.md": (
            "---\nname: pkg-skill\ndescription: Upgrade fixture skill\n---\n# pkg skill\n"
        ),
        ".apm/instructions/style.instructions.md": (
            '---\ndescription: Style\napplyTo: "**"\n---\nKeep it short.\n'
        ),
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")


def manifest_text(case: str) -> str:
    return (
        f"name: {case}\nversion: 1.0.0\n"
        f"{CASES[case]}"
        "dependencies:\n  apm:\n  - path: ../pkg\n" + MCP_BLOCK
    )


def wrapper(apm_src: Path, version: str) -> list[str]:
    # Stamp the producing version everywhere APM reports it (CLI banner and
    # lockfile ``apm_version``), not the version of the running virtualenv.
    code = (
        "import sys, importlib.metadata as md; sys.path.insert(0, sys.argv.pop(1)); "
        "ver = sys.argv.pop(1); real = md.version; "
        "md.version = lambda name: ver if name == 'apm-cli' else real(name); "
        "import apm_cli.version as v; v.__BUILD_VERSION__ = ver; "
        "from apm_cli.cli import main; sys.argv[0] = 'apm'; main()"
    )
    return [sys.executable, "-c", code, str(apm_src), version]


def copy_outputs(project: Path, dest: Path) -> None:
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if rel.parts[0] in ("apm_modules", ".git", ".apm-local"):
            continue
        if path.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apm-src", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    command = wrapper(args.apm_src.resolve(), args.version)
    out = args.out.resolve()
    shutil.rmtree(out, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="t2-upgrade-") as tmp:
        isolated = IsolatedApmEnvironment.create(Path(tmp) / "env", base_env=dict(os.environ))
        env = isolated.subprocess_env()
        write_package(isolated.work_root / "pkg")
        for case in CASES:
            project = isolated.work_root / case
            project.mkdir()
            (project / "apm.yml").write_text(manifest_text(case), encoding="ascii")
            if case in AUTO_DETECT_DIRS:
                (project / AUTO_DETECT_DIRS[case]).mkdir()
            result = subprocess.run(
                [*command, "install", "--no-policy"],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                print(result.stdout, result.stderr, sep="\n")
                return result.returncode
            copy_outputs(project, out / case)
            print(f"{case}: ok")
        write_package(out / "pkg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
