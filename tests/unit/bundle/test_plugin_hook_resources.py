"""Producer regressions for relocatable, discoverable Claude plugin hooks."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

import pytest

from apm_cli.bundle.plugin_exporter import export_plugin_bundle
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.fixture
def hook_project(tmp_path: Path) -> Path:
    """Author the two independent hook-resource failures from issue #3074."""
    factory = LocalPackageFactory(tmp_path)
    package = factory.create("demo", version="1.0.0")
    for name, event, relative in (
        ("skill", "Stop", ".apm/skills/demo/scripts/check.py"),
        ("script", "PostToolUse", "scripts/other.py"),
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
                                    "command": f'python3 "${{CLAUDE_PLUGIN_ROOT}}/{relative}"',
                                    "timeout": 30,
                                }
                            ]
                        }
                    ]
                }
            },
        )
    for relative, content in {
        ".apm/skills/demo/SKILL.md": "---\nname: demo\ndescription: Demo\n---\n# Demo\n",
        ".apm/skills/demo/scripts/check.py": 'raise RuntimeError("must not execute")\n',
        ".apm/hooks/scripts/other.py": 'raise RuntimeError("must not execute")\n',
        ".apm/hooks/scripts/support.txt": "support data\n",
    }.items():
        path = package.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return package.root


def test_pack_relocates_skill_command_and_preserves_hook_assets(hook_project: Path) -> None:
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    hooks = json.loads((result.bundle_path / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert hooks["Stop"][0]["hooks"][0] == {
        "type": "command",
        "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/skills/demo/scripts/check.py"',
        "timeout": 30,
    }
    assert hooks["PostToolUse"][0]["hooks"][0]["command"] == (
        'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/other.py"'
    )
    assert (result.bundle_path / "hooks/scripts/other.py").read_bytes() == (
        hook_project / ".apm/hooks/scripts/other.py"
    ).read_bytes()
    assert (result.bundle_path / "hooks/scripts/support.txt").read_text() == "support data\n"
    assert "hooks/scripts/support.txt" in result.files
    assert not (result.bundle_path / "hooks/script.json").exists()
    assert not (result.bundle_path / ".apm").exists()


def test_pack_declares_hooks_in_native_claude_manifest(hook_project: Path) -> None:
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    manifest = json.loads(
        (result.bundle_path / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["hooks"] == "./hooks.json"
    assert json.loads((result.bundle_path / manifest["hooks"]).read_text())["hooks"].keys() == {
        "Stop",
        "PostToolUse",
    }
    assert json.loads((result.bundle_path / "plugin.json").read_text()) == manifest
    assert not (result.bundle_path / "hooks/hooks.json").exists()


def test_pack_dry_run_lists_hook_resources_without_writing(hook_project: Path) -> None:
    result = export_plugin_bundle(hook_project, hook_project / "dist", dry_run=True)
    assert {
        "hooks/scripts/other.py",
        "hooks/scripts/support.txt",
        "hooks.json",
        ".claude-plugin/plugin.json",
    } <= set(result.files)
    assert not result.bundle_path.exists()


@pytest.mark.parametrize("reference", ["missing.py", "../outside.py", "scripts/linked.py"])
def test_pack_rejects_unavailable_hook_resource_before_replacing_output(
    hook_project: Path, reference: str
) -> None:
    outside = hook_project.parent / "outside.py"
    outside.write_text("not publishable", encoding="utf-8")
    (hook_project / ".apm/hooks/scripts/linked.py").symlink_to(outside)
    descriptor = hook_project / ".apm/hooks/script.json"
    descriptor.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"command": f'python "${{CLAUDE_PLUGIN_ROOT}}/{reference}"'}]}}
        ),
        encoding="utf-8",
    )
    output = hook_project / "dist/demo-1.0.0"
    output.mkdir(parents=True)
    (output / "keep.txt").write_text("previous bundle", encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)hook.*(resource|path|script)"):
        export_plugin_bundle(hook_project, output.parent)
    assert (output / "keep.txt").read_text() == "previous bundle"


def test_explicit_includes_require_referenced_assets(hook_project: Path) -> None:
    manifest = load_yaml(hook_project / "apm.yml")
    manifest["includes"] = [".apm/hooks/script.json"]
    dump_yaml(manifest, hook_project / "apm.yml")
    with pytest.raises(ValueError, match="includes"):
        export_plugin_bundle(hook_project, hook_project / "dist")

    manifest["includes"].append(".apm/hooks/scripts")
    dump_yaml(manifest, hook_project / "apm.yml")
    clear_apm_yml_cache()
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    assert (result.bundle_path / "hooks/scripts/other.py").is_file()
    assert not (result.bundle_path / "skills").exists()


@pytest.mark.parametrize("exclude_metadata", [False, True])
def test_hook_bundle_preserves_node_module_type(hook_project: Path, exclude_metadata: bool) -> None:
    (hook_project / ".apm/hooks/package.json").write_text(
        '{"type":"module","scripts":{"postinstall":"must-not-run"}}', encoding="utf-8"
    )
    descriptor = hook_project / ".apm/hooks/script.json"
    descriptor.write_text(
        '{"hooks":{"Stop":[{"command":"node ${CLAUDE_PLUGIN_ROOT}/scripts/other.js"}]}}',
        encoding="utf-8",
    )
    (hook_project / ".apm/hooks/scripts/other.js").write_text(
        'import "./helper.js";\n', encoding="utf-8"
    )
    (hook_project / ".apm/hooks/scripts/helper.js").write_text(
        'throw new Error("must not run");\n', encoding="utf-8"
    )
    if exclude_metadata:
        manifest = load_yaml(hook_project / "apm.yml")
        manifest["includes"] = [".apm/hooks/script.json", ".apm/hooks/scripts"]
        dump_yaml(manifest, hook_project / "apm.yml")
        with pytest.raises(ValueError, match="includes"):
            export_plugin_bundle(hook_project, hook_project / "dist")
        assert not (hook_project / "dist/demo-1.0.0").exists()
        return
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    assert (result.bundle_path / "hooks/package.json").read_bytes() == (
        hook_project / ".apm/hooks/package.json"
    ).read_bytes()
    assert (result.bundle_path / "hooks/scripts/helper.js").is_file()
    assert set(json.loads((result.bundle_path / "hooks.json").read_text())) == {"hooks"}


def test_root_native_hooks_keep_paths_and_use_descriptor_relative_fallback(
    hook_project: Path,
) -> None:
    shutil.move(hook_project / ".apm/hooks", hook_project / "hooks")
    shutil.move(hook_project / ".apm/skills", hook_project / "skills")
    (hook_project / ".apm").rmdir()
    descriptor = hook_project / "hooks/skill.json"
    descriptor.write_text(
        descriptor.read_text().replace("/.apm/skills/", "/skills/"), encoding="utf-8"
    )
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    hooks = json.loads((result.bundle_path / "hooks.json").read_text())["hooks"]
    assert hooks["Stop"][0]["hooks"][0]["command"] == (
        'python3 "${CLAUDE_PLUGIN_ROOT}/skills/demo/scripts/check.py"'
    )
    assert hooks["PostToolUse"][0]["hooks"][0]["command"] == (
        'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/other.py"'
    )
    assert (result.bundle_path / "hooks/scripts/support.txt").is_file()


@pytest.mark.parametrize("reference", ["skills/private/run.py", "skills/private"])
def test_hook_resource_reference_does_not_override_apm_source_authority(
    hook_project: Path, reference: str
) -> None:
    root_skill = hook_project / "skills/private/run.py"
    root_skill.parent.mkdir(parents=True)
    root_skill.write_text("not for publication", encoding="utf-8")
    descriptor = hook_project / ".apm/hooks/script.json"
    descriptor.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"command": f"python ${{CLAUDE_PLUGIN_ROOT}}/{reference}"}]}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="selected layout"):
        export_plugin_bundle(hook_project, hook_project / "dist")


def test_split_quoted_root_and_multiple_flat_command_fields(hook_project: Path) -> None:
    descriptor = hook_project / ".apm/hooks/script.json"
    command = 'python "${CLAUDE_PLUGIN_ROOT}"/scripts/other.py'
    descriptor.write_text(
        json.dumps({"hooks": {"custom/event~name": [{"bash": command, "powershell": command}]}}),
        encoding="utf-8",
    )
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    entry = json.loads((result.bundle_path / "hooks.json").read_text())["hooks"][
        "custom/event~name"
    ]
    assert entry == [
        {
            "bash": 'python "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/other.py"',
            "powershell": 'python "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/other.py"',
        }
    ]


@pytest.mark.parametrize("quoting", ["bare", "full", "split"])
@pytest.mark.parametrize("name", ["check file.py", "check;file.py"])
def test_hook_relocation_preserves_authored_shell_escapes(
    hook_project: Path, name: str, quoting: str
) -> None:
    source = hook_project / ".apm/hooks/scripts" / name
    source.write_text("raise RuntimeError('must not run')", encoding="utf-8")
    escaped = name.replace(" ", "\\ ").replace(";", "\\;")
    reference = {
        "bare": f"${{CLAUDE_PLUGIN_ROOT}}/scripts/{escaped}",
        "full": f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{name}"',
        "split": f'"${{CLAUDE_PLUGIN_ROOT}}"/scripts/{escaped}',
    }[quoting]
    (hook_project / ".apm/hooks/script.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"command": f"python {reference}"}]}}),
        encoding="utf-8",
    )
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    command = json.loads((result.bundle_path / "hooks.json").read_text())["hooks"]["PostToolUse"][
        0
    ]["command"]
    expected = (
        f"${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{escaped}"
        if quoting == "bare"
        else f'"${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{name}"'
    )
    assert command == f"python {expected}"
    assert shlex.split(command) == ["python", f"${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{name}"]
    assert (result.bundle_path / "hooks/scripts" / name).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("layout", ["apm", "root"])
@pytest.mark.parametrize("include_directory", [True, False])
def test_hook_directory_reference_uses_only_selected_materialized_files(
    hook_project: Path, layout: str, include_directory: bool
) -> None:
    source_relative = ".apm/skills/demo"
    if layout == "root":
        shutil.move(hook_project / ".apm/skills", hook_project / "skills")
        source_relative = "skills/demo"
    descriptor = hook_project / ".apm/hooks/script.json"
    descriptor.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "command": (
                                f'cd "${{CLAUDE_PLUGIN_ROOT}}/{source_relative}" '
                                "&& python scripts/check.py"
                            )
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = load_yaml(hook_project / "apm.yml")
    manifest["includes"] = [".apm/hooks/script.json"]
    if include_directory:
        manifest["includes"].append(source_relative)
    dump_yaml(manifest, hook_project / "apm.yml")
    if not include_directory:
        with pytest.raises(ValueError, match="includes"):
            export_plugin_bundle(hook_project, hook_project / "dist", dry_run=True)
        assert not (hook_project / "dist").exists()
        return

    result = export_plugin_bundle(hook_project, hook_project / "dist")
    command = json.loads((result.bundle_path / "hooks.json").read_text())["hooks"]["Stop"][0][
        "command"
    ]
    assert command == 'cd "${CLAUDE_PLUGIN_ROOT}/skills/demo" && python scripts/check.py'
    assert (result.bundle_path / "skills/demo/scripts/check.py").is_file()
    assert "skills/demo" not in result.files
    assert not (result.bundle_path / "hooks/scripts").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_empty_hook_directory_is_rejected_before_replacing_output(
    hook_project: Path, dry_run: bool
) -> None:
    directory = hook_project / ".apm/hooks/empty"
    directory.mkdir()
    (hook_project / ".apm/hooks/script.json").write_text(
        '{"hooks":{"Stop":[{"command":"cd ${CLAUDE_PLUGIN_ROOT}/.apm/hooks/empty"}]}}',
        encoding="utf-8",
    )
    manifest = load_yaml(hook_project / "apm.yml")
    manifest["includes"] = [".apm/hooks/script.json", ".apm/hooks/empty"]
    dump_yaml(manifest, hook_project / "apm.yml")
    output = hook_project / "dist/demo-1.0.0"
    output.mkdir(parents=True)
    sentinel = output / "keep.txt"
    sentinel.write_text("previous bundle", encoding="utf-8")
    with pytest.raises(
        ValueError, match="Hook resource directory has no selected files: hooks/empty"
    ):
        export_plugin_bundle(hook_project, output.parent, dry_run=dry_run)
    assert sentinel.read_text() == "previous bundle"


def test_unmapped_hook_directory_diagnostic_names_authored_directory(
    hook_project: Path,
) -> None:
    (hook_project / ".apm/custom/empty").mkdir(parents=True)
    (hook_project / ".apm/hooks/script.json").write_text(
        '{"hooks":{"Stop":[{"command":"cd ${CLAUDE_PLUGIN_ROOT}/.apm/custom/empty"}]}}',
        encoding="utf-8",
    )
    manifest = load_yaml(hook_project / "apm.yml")
    manifest["includes"] = [".apm/hooks/script.json", ".apm/custom/empty"]
    dump_yaml(manifest, hook_project / "apm.yml")
    with pytest.raises(ValueError, match="Hook resource has no packed layout") as error:
        export_plugin_bundle(hook_project, hook_project / "dist")
    assert str(error.value) == (
        "Hook resource has no packed layout: .apm/custom/empty. "
        "Move it under the selected hooks or primitive directory."
    )
    assert not (hook_project / "dist").exists()


@pytest.mark.parametrize("script_relative", ["scripts/run.js", "run.js"])
@pytest.mark.parametrize("local_metadata", [True, False])
def test_root_script_keeps_node_module_scope_without_publishing_package_tree(
    hook_project: Path, script_relative: str, local_metadata: bool
) -> None:
    source = hook_project / script_relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("throw new Error('must not run');\n", encoding="utf-8")
    (hook_project / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    if local_metadata:
        (source.parent / "package.json").write_text('{"type":"commonjs"}', encoding="utf-8")
    (hook_project / "private.txt").write_text("do not publish", encoding="utf-8")
    (hook_project / ".apm/hooks/script.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"command": f'node "${{CLAUDE_PLUGIN_ROOT}}/{script_relative}"'}]}}
        ),
        encoding="utf-8",
    )
    result = export_plugin_bundle(hook_project, hook_project / "dist")
    metadata = json.loads(
        (result.bundle_path / Path(script_relative).parent / "package.json").read_text()
    )
    assert metadata["type"] == ("commonjs" if local_metadata else "module")
    assert (result.bundle_path / script_relative).read_bytes() == source.read_bytes()
    assert not (result.bundle_path / "private.txt").exists()


@pytest.mark.parametrize(
    "reserved_path", ["plugin.json", "hooks.json", "plugin.json/probe.py", "hooks.json/probe.py"]
)
def test_hook_resource_cannot_replace_generated_output(
    hook_project: Path, reserved_path: str
) -> None:
    resource = hook_project / reserved_path
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("{}", encoding="utf-8")
    (hook_project / ".apm/hooks/script.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"command": f"cat ${{CLAUDE_PLUGIN_ROOT}}/{reserved_path}"}]}}
        ),
        encoding="utf-8",
    )
    output = hook_project / "dist/demo-1.0.0"
    output.mkdir(parents=True)
    sentinel = output / "keep.txt"
    sentinel.write_text("previous bundle", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicts with generated output"):
        export_plugin_bundle(hook_project, output.parent)
    assert sentinel.read_text() == "previous bundle"


def test_generated_hook_metadata_cannot_replace_a_dependency_file(hook_project: Path) -> None:
    script = hook_project / ".apm/skills/demo/scripts/run.js"
    script.write_text("throw new Error('must not run');\n", encoding="utf-8")
    (hook_project / ".apm/hooks/skill.json").write_text(
        '{"hooks":{"Stop":[{"command":"node ${CLAUDE_PLUGIN_ROOT}/.apm/skills/demo/scripts/run.js"}]}}',
        encoding="utf-8",
    )
    deployed = hook_project / ".claude/skills/demo/scripts/package.json"
    deployed.parent.mkdir(parents=True)
    deployed.write_text('{"type":"commonjs"}', encoding="utf-8")
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/metadata",
            deployed_files=[".claude/skills/demo/scripts/package.json"],
        )
    )
    lockfile.write(hook_project / "apm.lock.yaml")
    with pytest.raises(ValueError, match="metadata collides"):
        export_plugin_bundle(hook_project, hook_project / "dist")
    assert not (hook_project / "dist").exists()


def test_dependency_collision_cannot_substitute_a_first_party_hook_resource(
    hook_project: Path,
) -> None:
    deployed = hook_project / ".claude/hooks/scripts/other.py"
    deployed.parent.mkdir(parents=True)
    deployed.write_text("different dependency script", encoding="utf-8")
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/hook-dependency",
            deployed_files=[".claude/hooks/scripts/other.py"],
        )
    )
    lockfile.write(hook_project / "apm.lock.yaml")
    with pytest.raises(ValueError, match="Hook resource collision"):
        export_plugin_bundle(hook_project, hook_project / "dist")
    result = export_plugin_bundle(hook_project, hook_project / "dist", force=True)
    assert (result.bundle_path / "hooks/scripts/other.py").read_bytes() == (
        hook_project / ".apm/hooks/scripts/other.py"
    ).read_bytes()
    packed_lock = load_yaml(result.bundle_path / "apm.lock.yaml")
    assert {
        ".claude-plugin/plugin.json",
        "hooks.json",
        "hooks/scripts/other.py",
        "hooks/scripts/support.txt",
    } <= packed_lock["pack"]["bundle_files"].keys()
