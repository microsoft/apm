"""Bind packed hook relocation to the existing consumer placeholder contract."""

import json
import shlex
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.bundle.plugin_exporter import export_plugin_bundle
from apm_cli.integration.hook_integrator import HookIntegrator
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.mark.req("req-tg-012")
@pytest.mark.parametrize("filename", ["probe.py", "probe file.py"])
@pytest.mark.parametrize(
    "source_command",
    [
        'python "${CLAUDE_PLUGIN_ROOT}"/scripts/probe.py',
        r"python '${CLAUDE_PLUGIN_ROOT}'\scripts\probe.py",
        'python "${CLAUDE_PLUGIN_ROOT}/scripts/probe.py"',
    ],
)
def test_packed_hook_keeps_expandable_quoting_through_consumer_resolution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source_command: str, filename: str
) -> None:
    """Packed paths remain resolvable, and a missing packed script warns visibly."""
    factory = LocalPackageFactory(tmp_path / "producer with spaces")
    package = factory.create("producer", version="1.0.0")
    source_command = source_command.replace(
        "probe.py",
        filename if source_command.endswith('probe.py"') else filename.replace(" ", "\\ "),
    )
    factory.add_hook(
        package,
        "scripts",
        {"hooks": {"Stop": [{"command": source_command}]}},
    )
    factory.add_hook_asset(
        package, "scripts", PurePosixPath(filename), "raise RuntimeError('must not run')\n"
    )
    packed = export_plugin_bundle(package.root, tmp_path / "output").bundle_path
    hook = json.loads((packed / "hooks.json").read_text(encoding="utf-8"))
    packed_command = hook["hooks"]["Stop"][0]["command"]
    assert packed_command == f'python "${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{filename}"'

    integrator = HookIntegrator()
    command, resources = integrator._rewrite_command_for_target(
        packed_command, packed, "producer", "claude"
    )
    target = f".claude/hooks/producer/hooks/scripts/{filename}"
    assert command == f'python "${{CLAUDE_PROJECT_DIR}}/{target}"'
    assert shlex.split(command) == ["python", f"${{CLAUDE_PROJECT_DIR}}/{target}"]
    assert resources == [(packed / "hooks/scripts" / filename, target)]
    assert resources[0][0].is_file()
    assert "Hook script not found" not in capsys.readouterr().out

    resources[0][0].unlink()
    unresolved, resources = integrator._rewrite_command_for_target(
        packed_command, packed, "producer", "claude"
    )
    assert unresolved == packed_command
    assert resources == []
    assert "Hook script not found" in capsys.readouterr().out
