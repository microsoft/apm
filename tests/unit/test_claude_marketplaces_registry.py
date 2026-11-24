"""Claude Code's marketplace registry owner: edge cases of the per-machine entry (#5176).

APM only overwrites an entry it owns for this checkout: it adds an entry when
none exists under the name and keeps every existing one byte for byte. A
linked registry location, an unreadable registry or receipt, an invalid name
or an unusable ``CLAUDE_CONFIG_DIR`` is left alone with a warning and never
fails the command. Uninstall removes only an entry APM recorded that still
names the same root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.native_plugins.claude_marketplaces import sync_project_marketplace

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-claude-native-plugin-registration"),
]

_NAME = "apm-p-0000abcd"


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm"))
    root = tmp_path / "repo" / ".agents" / "plugins"
    root.mkdir(parents=True)
    return root


def _registry(tmp_path: Path) -> Path:
    return tmp_path / "claude" / "plugins" / "known_marketplaces.json"


def _receipt(tmp_path: Path) -> Path:
    return tmp_path / "apm" / "claude-known-marketplaces.json"


def _sync(root: Path, *, declared: bool = True, name: str = _NAME) -> list[str]:
    logger = _Logger()
    sync_project_marketplace(name, root, user_scope=False, claude_declared=declared, logger=logger)
    return logger.warnings


def _seed(path: Path, text: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.read_bytes()


def test_an_entry_under_the_name_for_another_root_stays_byte_identical(
    env: Path, tmp_path: Path
) -> None:
    """Another clone's or worktree's entry is never overwritten: keep and warn."""
    other = tmp_path / "other-clone" / ".agents" / "plugins"
    other.mkdir(parents=True)
    entry = {"source": {"source": "directory", "path": str(other)}, "installLocation": str(other)}
    before = _seed(_registry(tmp_path), json.dumps({_NAME: entry}, indent=2) + "\n")

    warnings = _sync(env)

    assert _registry(tmp_path).read_bytes() == before
    assert (
        len(warnings) == 1 and "only overwrites an entry it owns for this checkout" in warnings[0]
    )
    assert not _receipt(tmp_path).exists()


@pytest.mark.parametrize("linked", ["claude-dir", "plugins-dir", "registry-file"])
def test_a_linked_registry_location_is_never_written(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    real = tmp_path / "dotfiles"
    (real / "plugins").mkdir(parents=True)
    (real / "plugins" / "known_marketplaces.json").write_text("{}\n", encoding="utf-8")
    claude = tmp_path / "claude"
    if linked == "claude-dir":
        claude.symlink_to(real, target_is_directory=True)
    elif linked == "plugins-dir":
        claude.mkdir()
        (claude / "plugins").symlink_to(real / "plugins", target_is_directory=True)
    else:
        (claude / "plugins").mkdir(parents=True)
        (claude / "plugins" / "known_marketplaces.json").symlink_to(
            real / "plugins" / "known_marketplaces.json"
        )

    warnings = _sync(env)

    assert (real / "plugins" / "known_marketplaces.json").read_text("utf-8") == "{}\n"
    assert len(warnings) == 1 and "symbolic link" in warnings[0]


@pytest.mark.parametrize("text", ["// jsonc\n{}\n", "[1, 2]\n", "{not json"])
def test_an_unreadable_registry_is_never_clobbered(env: Path, tmp_path: Path, text: str) -> None:
    before = _seed(_registry(tmp_path), text)

    warnings = _sync(env)

    assert _registry(tmp_path).read_bytes() == before
    assert len(warnings) == 1 and _NAME in warnings[0]


def test_an_unreadable_receipt_is_never_overwritten(env: Path, tmp_path: Path) -> None:
    before = _seed(_receipt(tmp_path), "{broken")

    install_warnings = _sync(env)
    uninstall_warnings = _sync(env, declared=False)

    assert _receipt(tmp_path).read_bytes() == before
    assert not _registry(tmp_path).exists()
    assert len(install_warnings) == 1 and len(uninstall_warnings) == 1


def test_uninstall_keeps_an_entry_whose_receipt_names_another_root(
    env: Path, tmp_path: Path
) -> None:
    """Receipt mismatch: Claude repointed the entry, so it is no longer APM's to remove."""
    assert _sync(env) == []
    registry = _registry(tmp_path)
    data = json.loads(registry.read_text("utf-8"))
    data[_NAME]["source"]["path"] = data[_NAME]["installLocation"] = "/elsewhere/.agents/plugins"
    before = _seed(registry, json.dumps(data, indent=2) + "\n")

    assert _sync(env, declared=False) == []

    assert registry.read_bytes() == before
    assert not _receipt(tmp_path).exists()


def test_uninstall_without_a_receipt_keeps_the_entry(env: Path, tmp_path: Path) -> None:
    entry = {"source": {"source": "directory", "path": str(env)}, "installLocation": str(env)}
    before = _seed(_registry(tmp_path), json.dumps({_NAME: entry}) + "\n")

    assert _sync(env) == []
    assert _sync(env, declared=False) == []

    assert _registry(tmp_path).read_bytes() == before


def test_a_unicode_root_with_spaces_is_recorded_verbatim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("APM_HOME", str(tmp_path / "apm"))
    root = tmp_path / "my r\u00e9po" / ".agents" / "plugins"
    root.mkdir(parents=True)

    assert _sync(root) == []

    text = _registry(tmp_path).read_text("utf-8")
    assert json.loads(text)[_NAME]["source"]["path"] == str(root)
    assert "r\u00e9po" in text
    assert _sync(root, declared=False) == []
    assert _NAME not in json.loads(_registry(tmp_path).read_text("utf-8"))


def test_a_relative_claude_config_dir_warns_and_never_raises(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/claude")

    warnings = _sync(env) + _sync(env, declared=False)

    assert not (tmp_path / "relative").exists()
    assert any("CLAUDE_CONFIG_DIR" in warning for warning in warnings)
    assert all(warning.isascii() for warning in warnings)


def test_an_invalid_marketplace_name_is_never_written(env: Path, tmp_path: Path) -> None:
    warnings = _sync(env, name="apm-p-../../evil")

    assert not _registry(tmp_path).exists()
    assert len(warnings) == 1 and "not an APM project marketplace name" in warnings[0]


def test_claudes_own_entry_for_this_root_is_kept_and_not_removed(env: Path, tmp_path: Path) -> None:
    entry = {"source": {"source": "directory", "path": str(env)}, "installLocation": "x"}
    before = _seed(_registry(tmp_path), json.dumps({_NAME: entry}) + "\n")

    assert _sync(env) == []
    assert _sync(env, declared=False) == []

    assert _registry(tmp_path).read_bytes() == before


def test_a_receipt_that_cannot_be_written_publishes_no_entry(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt is recorded first: if it cannot be written, no entry is published."""
    from apm_cli.native_plugins import claude_marketplaces

    real_write = claude_marketplaces.atomic_write_text

    def write(path, text, *args, **kwargs):
        if Path(path) == _receipt(tmp_path):
            raise OSError("disk full")
        return real_write(path, text, *args, **kwargs)

    monkeypatch.setattr(claude_marketplaces, "atomic_write_text", write)

    warnings = _sync(env)

    assert not _registry(tmp_path).exists()
    assert len(warnings) == 1 and "disk full" in warnings[0]


def test_uninstall_under_another_claude_profile_leaves_that_profile_alone(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _sync(env) == []
    receipt = _receipt(tmp_path).read_bytes()
    other = tmp_path / "claude-b" / "plugins" / "known_marketplaces.json"
    entry = {"source": {"source": "directory", "path": str(env)}, "installLocation": str(env)}
    before = _seed(other, json.dumps({_NAME: entry}) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-b"))

    assert _sync(env, declared=False) == []

    assert other.read_bytes() == before
    assert _receipt(tmp_path).read_bytes() == receipt
    assert _NAME in json.loads(_registry(tmp_path).read_text("utf-8"))
