"""Hermetic installed-CLI proof of the Claude plugin hook producer contract."""

from __future__ import annotations

import json
import os
import shlex
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.lifecycle_smoke, pytest.mark.requires_apm_binary]


def _read_claude_hooks(bundle: Path) -> dict:
    """Follow Claude's documented manifest location and declaration, without executing it."""
    manifest = json.loads((bundle / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "hook-producer"
    assert manifest["hooks"] == "./hooks.json"
    hooks = json.loads((bundle / manifest["hooks"]).read_text(encoding="utf-8"))
    assert set(hooks["hooks"]) == {"Stop", "PostToolUse"}
    for matchers in hooks["hooks"].values():
        command = matchers[0]["hooks"][0]["command"]
        resource = shlex.split(command)[1].removeprefix("${CLAUDE_PLUGIN_ROOT}/")
        assert resource.split("/")[0] != ".apm"
        assert (bundle / resource).resolve().is_relative_to(bundle.resolve())
        assert (bundle / resource).is_file()
    return hooks


def test_pack_archive_repack_and_rejection_preserve_complete_hook_bundle(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    package = factory.create("hook-producer", version="1.0.0")
    manifest = load_yaml(package.manifest_path)
    manifest["dependencies"] = {}
    dump_yaml(manifest, package.manifest_path)
    marker = isolated.work_root / "hook-executed.txt"
    script_content = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    for name, event, reference in (
        ("script", "Stop", "script/run file.py"),
        ("skill", "PostToolUse", ".apm/skills/demo/scripts/check file.py"),
    ):
        factory.add_hook(
            package,
            name,
            {
                "hooks": {
                    event: [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'python "${{CLAUDE_PLUGIN_ROOT}}/{reference}"',
                                }
                            ]
                        }
                    ]
                }
            },
        )
    script = factory.add_hook_asset(
        package, "script", PurePosixPath("run file.py"), script_content, executable=True
    )
    resource = factory.add_hook_asset(
        package, "script", PurePosixPath("support.json"), '{"fixture":"v1"}'
    )
    skill = package.root / ".apm/skills/demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Fixture skill\n---\n# Demo\n", encoding="utf-8"
    )
    (skill / "scripts/check file.py").write_text(script_content, encoding="utf-8")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    environment = isolated.subprocess_env()
    output = package.root / "build/hook-producer-1.0.0"
    base_args = ("pack", "--format", "plugin")

    dry_run = runner.run(
        (*base_args, "--dry-run", "--verbose"),
        cwd=package.root,
        env=environment,
        scenario_id="hook-producer-dry-run",
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert "hooks/script/support.json" in dry_run.stdout
    assert not output.exists()

    for phase in ("initial", "repack"):
        if phase == "repack":
            resource.write_text('{"fixture":"v2"}', encoding="utf-8")
            (output / "stale.txt").write_text("old output", encoding="utf-8")
        result = runner.run(
            base_args, cwd=package.root, env=environment, scenario_id=f"hook-producer-{phase}"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        _read_claude_hooks(output)
        assert (output / "hooks/script/support.json").read_bytes() == resource.read_bytes()
        assert (output / "hooks/script/run file.py").read_bytes() == script.read_bytes()
        assert not (output / "stale.txt").exists()
        assert not marker.exists()

    archive = runner.run(
        (*base_args, "--archive", "-o", "archives"),
        cwd=package.root,
        env=environment,
        scenario_id="hook-producer-archive",
    )
    assert archive.returncode == 0, archive.stdout + archive.stderr
    with zipfile.ZipFile(package.root / "archives/hook-producer-1.0.0.zip") as packed:
        prefix = "hook-producer-1.0.0/"
        manifest = json.loads(packed.read(prefix + ".claude-plugin/plugin.json"))
        assert manifest["hooks"] == "./hooks.json"
        assert packed.read(prefix + "hooks/script/support.json") == resource.read_bytes()
        assert packed.read(prefix + "hooks/script/run file.py") == script.read_bytes()
        assert packed.read(prefix + "skills/demo/scripts/check file.py") == script_content.encode()
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    script.unlink()
    script.symlink_to(marker.parent / "outside.py")
    (marker.parent / "outside.py").write_text("outside package", encoding="utf-8")
    rejected = runner.run(
        base_args, cwd=package.root, env=environment, scenario_id="hook-producer-containment"
    )
    assert rejected.returncode != 0
    assert "Hook resource" in rejected.stdout + rejected.stderr
    assert {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    } == before
    assert not marker.exists()
