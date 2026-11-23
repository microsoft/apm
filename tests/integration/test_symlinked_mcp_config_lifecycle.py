"""Project MCP configs are only merged into regular files inside the project.

When ``.mcp.json`` (Claude Code and Copilot), ``.kiro/settings/mcp.json`` or
``.codex/config.toml`` (or its parent directory) is a symlink,
``apm install`` skips that runtime's MCP write with a warning, reports the
runtime as failed, and leaves the link target unchanged.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.integration._containment_kit import build_containment_kit, output, snapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
    pytest.mark.e2e,
]


@pytest.mark.parametrize(
    ("target", "config_rel", "seed"),
    [
        ("claude", ".mcp.json", '{"mcpServers": {}}\n'),
        ("copilot", ".mcp.json", '{"mcpServers": {}}\n'),
        ("kiro", ".kiro/settings/mcp.json", '{"mcpServers": {}}\n'),
        ("codex", ".codex/config.toml", 'model = "x"\n'),
    ],
)
def test_install_skips_linked_project_mcp_config(
    tmp_path: Path, apm_binary_path: Path, target: str, config_rel: str, seed: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest((target,), mcp=True)
    (kit.project / ".claude").mkdir()
    linked = kit.outside / "config.json"
    linked.write_text(seed, encoding="utf-8")
    config = kit.project / config_rel
    config.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(linked, config)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "does not write MCP config through symlinks" in output(result)
    assert linked.read_text(encoding="utf-8") == seed
    assert config.is_symlink()


@pytest.mark.parametrize(
    ("target", "config_rel"),
    [
        ("claude", ".mcp.json"),
        ("copilot", ".mcp.json"),
        ("kiro", ".kiro/settings/mcp.json"),
        ("codex", ".codex/config.toml"),
    ],
)
def test_install_writes_regular_project_mcp_config(
    tmp_path: Path, apm_binary_path: Path, target: str, config_rel: str
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest((target,), mcp=True)
    (kit.project / ".claude").mkdir()
    (kit.project / ".kiro").mkdir()
    (kit.project / ".codex").mkdir()

    result = kit.run("install", "--no-policy")

    assert result.returncode == 0, output(result)
    assert "localsrv" in (kit.project / config_rel).read_text(encoding="utf-8")


_MCP_LINK = "does not write MCP config through symlinks"
# A linked harness root is refused before any write, MCP included.
_ROOT_LINK = "Refusing deployment through symlinked target root: .codex"


@pytest.mark.parametrize(
    ("target", "parent", "link", "report"),
    [("kiro", ".kiro", ".kiro/settings", _MCP_LINK), ("codex", None, ".codex", _ROOT_LINK)],
)
def test_install_skips_linked_project_mcp_config_directory(
    tmp_path: Path,
    apm_binary_path: Path,
    target: str,
    parent: str | None,
    link: str,
    report: str,
) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest((target,), mcp=True)
    if parent:
        (kit.project / parent).mkdir()
    os.symlink(kit.outside, kit.project / link, target_is_directory=True)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert report in output(result)
    assert not any(kit.outside.iterdir())


def test_removing_a_server_through_a_linked_codex_dir_reports_it_and_deletes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Retiring a server whose config dir became a link outside is reported, not applied."""
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("codex",), mcp=True)
    (kit.project / ".codex").mkdir()
    installed = kit.run("install", "--no-policy")
    assert installed.returncode == 0, output(installed)
    copy = kit.outside / ".codex"
    shutil.copytree(kit.project / ".codex", copy)
    shutil.rmtree(kit.project / ".codex")
    os.symlink(copy, kit.project / ".codex", target_is_directory=True)
    before = snapshot(kit.outside)
    assert "localsrv" in before[".codex/config.toml"].decode("utf-8")

    kit.write_manifest(("codex",), mcp=False)
    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert snapshot(kit.outside) == before, output(result)
    assert (kit.project / ".codex").is_symlink()
    assert _ROOT_LINK in output(result)


_CODEX_SEED = 'model = "x"\n'


def _link_codex(kit, link: str) -> Path:
    """Link the project's ``.codex`` dir or its ``config.toml`` outside; return the target."""
    if link == "dir":
        target = kit.outside / "codex"
        target.mkdir()
        (target / "config.toml").write_text(_CODEX_SEED, encoding="utf-8")
        os.symlink(target, kit.project / ".codex", target_is_directory=True)
        return target / "config.toml"
    (kit.project / ".codex").mkdir()
    target = kit.outside / "config.toml"
    target.write_text(_CODEX_SEED, encoding="utf-8")
    os.symlink(target, kit.project / ".codex" / "config.toml")
    return target


@pytest.mark.parametrize("link", ["dir", "file"])
def test_install_mcp_flag_refuses_a_linked_codex_config(
    tmp_path: Path, apm_binary_path: Path, link: str
) -> None:
    """``apm install --mcp`` writes through the same link policy as the install pipeline."""
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("codex",))
    target = _link_codex(kit, link)

    result = kit.run("install", "--no-policy", "--verbose", "--mcp", "localsrv", "--", "echo", "ok")

    assert target.read_text(encoding="utf-8") == _CODEX_SEED, output(result)
    assert "symlink" in output(result), output(result)
    linked = kit.project / ".codex" if link == "dir" else kit.project / ".codex/config.toml"
    assert linked.is_symlink()


def test_uninstall_reports_a_server_behind_a_linked_codex_config_file(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A server whose config.toml became a link outside is reported; nothing outside changes."""
    kit = build_containment_kit(tmp_path, apm_binary_path)
    (kit.package / "apm.yml").write_text(
        "name: pkg\nversion: 1.0.0\ndependencies:\n  mcp:\n    - name: pkgsrv\n"
        "      registry: false\n      transport: stdio\n      command: echo\n"
        "      args: [ok]\n",
        encoding="utf-8",
    )
    kit.write_manifest(("codex",))
    (kit.project / ".codex").mkdir()
    installed = kit.run("install", "--no-policy", "--trust-transitive-mcp")
    config = kit.project / ".codex" / "config.toml"
    assert "pkgsrv" in config.read_text(encoding="utf-8"), output(installed)
    copy = kit.outside / "config.toml"
    shutil.copyfile(config, copy)
    config.unlink()
    os.symlink(copy, config)
    before = snapshot(kit.outside)

    result = kit.run("uninstall", kit.package.as_posix())

    assert snapshot(kit.outside) == before, output(result)
    assert config.is_symlink()
    assert "symlink" in output(result), output(result)
