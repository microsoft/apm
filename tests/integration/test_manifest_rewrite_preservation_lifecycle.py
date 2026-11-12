"""Installed-CLI contract: every apm.yml rewrite keeps the user's authoring.

``apm install PKG`` and ``apm uninstall PKG`` stopped stripping comments in
#2000/#2012. The other commands that rewrite the consumer manifest --
``apm approve``, ``apm deny`` and ``apm install --mcp`` -- must honour the
same promise: comments, flow-style lists, anchors/aliases, unknown
extension fields and top-level key order survive, and an install/uninstall
round trip restores the original bytes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_MANIFEST = """\
# Consumer manifest: hand-written, keep these notes.
name: consumer   # project name
version: 0.1.0
x-shared:
  rule: &rule ./pkgs/alpha   # anchored entry
target: [claude]   # flow-style list
dependencies:
  # Direct APM packages
  apm:
    - *rule
executables: {}   # executable gate on
"""
_COMMENTS = tuple(line.split("#", 1)[1].strip() for line in _MANIFEST.splitlines() if "#" in line)
_TOP_LEVEL = ("name", "version", "x-shared", "target", "dependencies", "executables")


def _package(root: Path, name: str, *, hook: bool) -> None:
    root.mkdir(parents=True)
    dump_yaml({"name": name, "version": "1.0.0"}, root / "apm.yml")
    rules = root / ".apm" / "instructions"
    rules.mkdir(parents=True)
    (rules / f"{name}.instructions.md").write_text(
        f'---\napplyTo: "**"\n---\n# {name}\n', encoding="ascii"
    )
    if hook:
        hooks = root / ".apm" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "guard.json").write_text(
            '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": '
            '[{"type": "command", "command": "echo guard"}]}]}}\n',
            encoding="ascii",
        )


def _assert_authoring_kept(manifest: Path, step: str) -> str:
    text = manifest.read_text(encoding="utf-8")
    missing = [comment for comment in _COMMENTS if comment not in text]
    assert not missing, f"{step}: comments dropped {missing}\n{text}"
    assert "target: [claude]" in text, f"{step}: flow style lost\n{text}"
    assert "&rule ./pkgs/alpha" in text and "- *rule" in text, f"{step}: alias lost\n{text}"
    order = [line.split(":", 1)[0] for line in text.splitlines() if line[:1].isalpha()]
    assert tuple(key for key in order if key in _TOP_LEVEL) == _TOP_LEVEL, f"{step}: {order}"
    return text


def test_manifest_rewrites_preserve_comments_layout_and_aliases(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    _package(project / "pkgs" / "alpha", "alpha", hook=False)
    _package(project / "pkgs" / "hooky", "hooky", hook=True)
    manifest = project / "apm.yml"
    manifest.write_text(_MANIFEST, encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(step: str, *args: str) -> str:
        result = runner.run(args, cwd=project, env=env)
        assert result.returncode == 0, f"{step}\n{result.stdout}{result.stderr}"
        return _assert_authoring_kept(manifest, step)

    assert apm("install", "install", "--no-policy") == _MANIFEST

    added = apm("install-package", "install", "--no-policy", "./pkgs/hooky")
    assert "./pkgs/hooky" in added

    approved = apm("approve", "approve", "./pkgs/hooky")
    assert "allow:" in approved

    denied = apm("deny", "deny", "./pkgs/hooky")
    assert "deny:" in denied

    with_mcp = apm("install-mcp", "install", "--no-policy", "--mcp", "local-echo", "--", "echo")
    assert "local-echo" in with_mcp

    removed = apm("uninstall-package", "uninstall", "./pkgs/hooky")
    assert "./pkgs/hooky" not in removed.split("executables:", 1)[0]

    rerun = apm("reinstall", "install", "--no-policy")
    assert rerun == removed
