"""Local bundle install deploys only primitive files for each target (#3649).

A local plugin bundle lists every file it ships in ``pack.bundle_files``.
Install routes each listed file to a target only when it lands in a
primitive directory that target owns (skills, agents, instructions, ...).
Files directly under a target root (runtime settings, MCP configuration) and
directories no primitive owns are skipped with a warning. Target-rooted hook
files follow the executables gate like hooks from any other package.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_SETTINGS = json.dumps(
    {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}
)
_MCP = json.dumps({"mcpServers": {"bundle-server": {"command": "true"}}})
_COPILOT_HOOK = json.dumps(
    {"version": 1, "hooks": {"sessionStart": [{"type": "command", "bash": "true"}]}}
)
_NON_PRIMITIVE = {
    ".claude/settings.json": _SETTINGS,
    ".claude/settings.local.json": _SETTINGS,
    ".cursor/mcp.json": _MCP,
    ".github/workflows/ci.yml": "on: push\n",
    "settings.json": _SETTINGS,
}


def _bundle(root: Path, files: dict[str, str]) -> Path:
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "plugin.json").write_text(json.dumps({"name": "bx", "version": "1.0.0"}), "utf-8")
    files = {"skills/guide/SKILL.md": "---\nname: guide\ndescription: d\n---\nGuide\n", **files}
    digests: dict[str, str] = {}
    for rel, text in files.items():
        path = bundle / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digests[rel] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    lock = {
        "lockfile_version": "1",
        "dependencies": [],
        "pack": {"format": "plugin", "bundle_files": digests},
    }
    (bundle / "apm.lock.yaml").write_text(yaml.safe_dump(lock), encoding="utf-8")
    return bundle


def _project_files(chain: Chain) -> set[str]:
    return {
        path.relative_to(chain.project).as_posix()
        for path in chain.project.rglob("*")
        if path.is_file() and "apm_modules" not in path.parts
    }


def test_bundle_install_routes_only_primitive_files(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = Chain(tmp_path / "world", apm_binary_path, dirs=(".claude", ".github", ".cursor"))
    chain.write_manifest([], targets=["claude", "copilot", "cursor"])
    bundle = _bundle(tmp_path, {**_NON_PRIMITIVE, ".github/prompts/review.prompt.md": "Review\n"})

    result = chain.ok("install", str(bundle), "--no-policy")

    files = _project_files(chain)
    assert ".claude/skills/guide/SKILL.md" in files
    assert ".github/prompts/review.prompt.md" in files
    for rel in (*_NON_PRIMITIVE, ".claude/settings.json", ".github/settings.json"):
        assert rel not in files, result.stdout
    assert "not a primitive file" in result.stdout + result.stderr


@pytest.mark.parametrize("gate", ["off", "on"])
def test_bundle_hook_files_follow_the_executables_gate(
    tmp_path: Path, apm_binary_path: Path, gate: str
) -> None:
    chain = Chain(tmp_path / "world", apm_binary_path, dirs=(".github",))
    extra = {"executables": {}} if gate == "on" else {}
    chain.write_manifest([], targets=["copilot"], **extra)
    bundle = _bundle(tmp_path, {".github/hooks/session.json": _COPILOT_HOOK})

    chain.ok("install", str(bundle), "--no-policy")

    deployed = (chain.project / ".github" / "hooks" / "session.json").exists()
    assert deployed is (gate == "off")
    assert (chain.project / ".agents" / "skills" / "guide" / "SKILL.md").exists()
