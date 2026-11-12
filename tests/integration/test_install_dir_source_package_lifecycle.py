"""Installed-CLI contract for ``apm install DIR`` on source packages (#3443).

A directory with an ``apm.yml`` and a root ``plugin.json`` but no ``apm pack``
manifest is a source package, so ``apm install DIR`` must record it as a local
dependency exactly like ``apm install DIR DIR``. Real ``apm pack`` output
(directory and archive) and legacy manifestless bundles must keep installing
through the local-bundle path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_SKILL = "---\nname: demo\ndescription: Demo skill.\n---\n# Demo\n"
_DEPLOYED_SKILL = Path(".agents/skills/demo/SKILL.md")
_LEGACY_BUNDLE_WARNING = "produced by an older APM version"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("ascii"))


def _source_package(root: Path, *, manifest: dict[str, object] | None = None) -> Path:
    """Author the #3443 layout: apm.yml + .apm/skills + root plugin.json."""
    package = root / "demo-pkg"
    package.mkdir(parents=True)
    dump_yaml(manifest or {"name": "demo-pkg", "version": "1.0.0"}, package / "apm.yml")
    _write(package / ".apm" / "skills" / "demo" / "SKILL.md", _SKILL)
    _write(
        package / "plugin.json",
        json.dumps({"name": "demo-pkg", "version": "1.0.0", "skills": ".apm/skills/"}) + "\n",
    )
    return package


def _consumer(root: Path, name: str) -> Path:
    project = root / name
    project.mkdir(parents=True)
    dump_yaml(
        {"name": "consumer", "version": "1.0.0", "dependencies": {"apm": []}},
        project / "apm.yml",
    )
    return project


def _installed_tree(root: Path) -> dict[str, bytes]:
    """Snapshot installed files, minus the lockfile and per-install local state."""
    tree = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if (
            path.is_file()
            and relative != "apm.lock.yaml"
            and not relative.startswith(".apm-local/")
        ):
            tree[relative] = path.read_bytes()
    return tree


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


class _Cli:
    def __init__(self, root: Path, apm_binary_path: Path) -> None:
        self._isolated = IsolatedApmEnvironment.create(root / "env", base_env=dict(os.environ))
        self._runner = ApmLifecycleRunner((str(apm_binary_path),))
        self.work_root = self._isolated.work_root

    def run(self, *args: str, cwd: Path, scenario_id: str) -> CommandResult:
        result = self._runner.run(
            args,
            scenario_id=scenario_id,
            cwd=cwd,
            env=self._isolated.subprocess_env(),
        )
        assert result.returncode == 0, _output(result)
        return result


def test_single_argument_dir_install_matches_two_argument_dependency_install(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """``apm install DIR`` records the source package like ``apm install DIR DIR``."""
    cli = _Cli(tmp_path, apm_binary_path)
    _source_package(cli.work_root)
    single = _consumer(cli.work_root, "single")
    double = _consumer(cli.work_root, "double")

    single_result = cli.run(
        "install",
        "--target",
        "copilot",
        "../demo-pkg",
        cwd=single,
        scenario_id="dir-source-package-single-arg",
    )
    cli.run(
        "install",
        "--target",
        "copilot",
        "../demo-pkg",
        "../demo-pkg",
        cwd=double,
        scenario_id="dir-source-package-double-arg",
    )

    output = _output(single_result)
    assert _LEGACY_BUNDLE_WARNING not in output
    assert "local bundle" not in output
    assert not (single / ".github" / ".apm").exists()
    assert not (single / ".github" / "apm.yml").exists()

    manifest = load_yaml(single / "apm.yml")
    assert manifest["dependencies"]["apm"] == ["../demo-pkg"]
    lock = load_yaml(single / "apm.lock.yaml")
    (dependency,) = lock["dependencies"]
    assert dependency["source"] == "local"
    assert dependency["local_path"] == "../demo-pkg"
    assert (single / _DEPLOYED_SKILL).read_text(encoding="utf-8") == _SKILL

    assert load_yaml(double / "apm.yml") == manifest
    assert load_yaml(double / "apm.lock.yaml")["dependencies"] == lock["dependencies"]
    assert _installed_tree(single) == _installed_tree(double)


@pytest.mark.parametrize("archive", (False, True), ids=("directory", "archive"))
def test_apm_pack_output_still_installs_as_local_bundle(
    tmp_path: Path,
    apm_binary_path: Path,
    archive: bool,
) -> None:
    """Real ``apm pack`` output keeps the local-bundle install path."""
    cli = _Cli(tmp_path, apm_binary_path)
    producer = _source_package(
        cli.work_root,
        manifest={
            "name": "demo-pkg",
            "version": "1.0.0",
            "license": "MIT",
            "dependencies": {"apm": []},
        },
    )
    cli.run("install", "--target", "copilot", cwd=producer, scenario_id="pack-producer-install")
    pack_args = ["pack", "--offline", "-o", "dist"]
    if archive:
        pack_args.append("--archive")
    cli.run(*pack_args, cwd=producer, scenario_id="pack-producer-pack")
    (bundle,) = (producer / "dist").iterdir()
    if not archive:
        assert (bundle / "plugin.json").is_file()
        assert "pack" in load_yaml(bundle / "apm.lock.yaml")

    consumer = _consumer(cli.work_root, "consumer")
    manifest_before = (consumer / "apm.yml").read_bytes()
    result = cli.run(
        "install",
        "--target",
        "copilot",
        str(bundle),
        cwd=consumer,
        scenario_id="pack-output-install",
    )

    assert "local bundle" in _output(result)
    assert (consumer / "apm.yml").read_bytes() == manifest_before
    assert load_yaml(consumer / "apm.lock.yaml")["dependencies"] == []
    assert (consumer / _DEPLOYED_SKILL).read_text(encoding="utf-8") == _SKILL


def test_legacy_manifestless_bundle_keeps_bundle_install(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A plugin.json directory without apm.yml or lockfile stays a legacy bundle."""
    cli = _Cli(tmp_path, apm_binary_path)
    legacy = cli.work_root / "legacy"
    _write(legacy / "plugin.json", json.dumps({"name": "legacy"}) + "\n")
    _write(legacy / "skills" / "demo" / "SKILL.md", _SKILL)
    consumer = _consumer(cli.work_root, "consumer")

    result = cli.run(
        "install",
        "--target",
        "copilot",
        "../legacy",
        cwd=consumer,
        scenario_id="legacy-manifestless-bundle-install",
    )

    output = _output(result)
    assert _LEGACY_BUNDLE_WARNING in output
    assert "local bundle" in output
    assert load_yaml(consumer / "apm.yml")["dependencies"]["apm"] == []
    assert (consumer / _DEPLOYED_SKILL).read_text(encoding="utf-8") == _SKILL
