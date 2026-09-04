"""Claude global deduplication and explicit cleanup preserve user content."""

from pathlib import Path

import pytest

from apm_cli.compilation.user_root_context import compile_user_root_contexts
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.primitives.discovery import clear_discovery_cache


@pytest.fixture
def global_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate home and use an external Claude config root with real primitives."""
    home = tmp_path / "home"
    source = home / ".apm"
    config = tmp_path / "external-claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    instructions = source / "apm_modules" / "demo" / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    for name in ("alpha", "beta"):
        (instructions / f"{name}.instructions.md").write_text(
            f"---\ndescription: {name} guidance\n---\nUse {name} conventions.\n",
            encoding="utf-8",
        )
    clear_discovery_cache()
    yield source, config, home / ".codex"
    clear_discovery_cache()


def _native_rules(config: Path) -> None:
    """Place the equivalent native rules for the fixture instructions."""
    rules = config / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    for name in ("alpha", "beta"):
        (rules / f"{name}.md").write_text(f"Use {name} conventions.\n", encoding="utf-8")


@pytest.mark.parametrize(
    "rule_content",
    [
        None,
        "Use alpha conventions.\n",
        "Outdated instructions.\n",
        "---\npaths: ['*.py']\n---\nUse alpha conventions.\n",
    ],
    ids=["unrelated-only", "partial", "stale", "scoped"],
)
def test_only_equivalent_instructions_are_omitted(global_tree, rule_content: str | None) -> None:
    """Neither unrelated rules nor a partially covered set suppress missing guidance."""
    source, config, codex = global_tree
    rules = config / "rules"
    rules.mkdir(parents=True)
    (rules / "unrelated.md").write_text("Unrelated rule.\n", encoding="utf-8")
    if rule_content is not None:
        (rules / "alpha.md").write_text(rule_content, encoding="utf-8")
    results = compile_user_root_contexts([KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]], source)
    assert all(result.status == "written" for result in results)
    claude_body = (config / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Use beta conventions." in claude_body
    assert ("Use alpha conventions." in claude_body) == (rule_content != "Use alpha conventions.\n")
    codex_body = (codex / "AGENTS.md").read_text(encoding="utf-8")
    assert "Use alpha conventions." in codex_body
    assert "Use beta conventions." in codex_body


@pytest.mark.parametrize("clean", [False, True])
def test_full_native_coverage_never_creates_root(global_tree, clean: bool) -> None:
    """Clean and normal compiles both leave native-only installations native-only."""
    source, config, _ = global_tree
    _native_rules(config)
    results = compile_user_root_contexts([KNOWN_TARGETS["claude"]], source, clean=clean)
    assert results[0].status == "skipped-native-rules"
    assert not (config / "CLAUDE.md").exists()
    assert len(list((config / "rules").iterdir())) == 2


def test_force_instructions_overrides_full_native_coverage(global_tree) -> None:
    """The existing force flag can still create a root fallback in global mode."""
    source, config, _ = global_tree
    _native_rules(config)

    results = compile_user_root_contexts(
        [KNOWN_TARGETS["claude"]],
        source,
        force_instructions=True,
    )

    assert results[0].status == "written"
    body = (config / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Use alpha conventions." in body
    assert "Use beta conventions." in body


def test_full_native_coverage_still_blocks_critical_hidden_characters(global_tree) -> None:
    """Security scanning runs even when native coverage suppresses the root write."""
    source, config, _ = global_tree
    instruction = (
        source / "apm_modules" / "demo" / ".apm" / "instructions" / "alpha.instructions.md"
    )
    hidden = "Use alpha \u202e conventions.\n"
    instruction.write_text("---\ndescription: alpha guidance\n---\n" + hidden, encoding="utf-8")
    rules = config / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "alpha.md").write_text(hidden, encoding="utf-8")
    (rules / "beta.md").write_text("Use beta conventions.\n", encoding="utf-8")
    clear_discovery_cache()

    result = compile_user_root_contexts([KNOWN_TARGETS["claude"]], source)[0]

    assert result.status == "error:critical hidden characters in compiled output"
    assert result.has_critical_security is True
    assert not (config / "CLAUDE.md").exists()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("clean", [False, True])
def test_existing_duplicate_requires_explicit_cleanup(
    global_tree, clean: bool, dry_run: bool
) -> None:
    """Only live --clean removes the old generated duplicate; repeated cleanup is a no-op."""
    source, config, codex = global_tree
    targets = [KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]]
    compile_user_root_contexts(targets, source)
    root = config / "CLAUDE.md"
    original = root.read_bytes()
    codex_original = (codex / "AGENTS.md").read_bytes()
    _native_rules(config)
    results = compile_user_root_contexts(targets, source, clean=clean, dry_run=dry_run)
    if clean and not dry_run:
        assert results[0].status == "removed"
        assert not root.exists()
        assert (
            compile_user_root_contexts(targets, source, clean=True)[0].status
            == "skipped-native-rules"
        )
    else:
        assert results[0].status == ("would-remove" if clean else "retained-redundant")
        assert root.read_bytes() == original
    assert (codex / "AGENTS.md").read_bytes() == codex_original
    for name in ("alpha", "beta"):
        assert (config / "rules" / f"{name}.md").read_text(
            encoding="utf-8"
        ) == f"Use {name} conventions.\n"


@pytest.mark.parametrize("kind", ["hand-authored", "edited", "older", "invalid-utf8"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cleanup_preserves_unverifiable_content(global_tree, kind: str, dry_run: bool) -> None:
    """A marker alone is insufficient proof that a root is safe to delete."""
    source, config, _ = global_tree
    targets = [KNOWN_TARGETS["claude"]]
    compile_user_root_contexts(targets, source)
    root = config / "CLAUDE.md"
    if kind == "hand-authored":
        root.write_text("My own guidance.\n", encoding="utf-8")
    elif kind == "edited":
        root.write_bytes(root.read_bytes() + b"Keep my additions.\n")
    elif kind == "older":
        root.write_bytes(root.read_bytes().replace(b"Use alpha", b"Use older"))
    else:
        root.write_bytes(b"\xff")
    _native_rules(config)
    before = root.read_bytes()
    result = compile_user_root_contexts(targets, source, clean=True, dry_run=dry_run)[0]
    assert result.status not in {"removed", "would-remove"}
    assert root.read_bytes() == before


@pytest.mark.parametrize("outside", [False, True])
def test_cleanup_preserves_root_symlinks(global_tree, tmp_path: Path, outside: bool) -> None:
    """Neither a contained nor an escaping root symlink grants deletion authority."""
    source, config, _ = global_tree
    targets = [KNOWN_TARGETS["claude"]]
    compile_user_root_contexts(targets, source)
    root = config / "CLAUDE.md"
    original = root.read_bytes()
    destination = (tmp_path if outside else config) / "owned-by-user.md"
    root.rename(destination)
    root.symlink_to(destination)
    _native_rules(config)
    result = compile_user_root_contexts(targets, source, clean=True)[0]
    assert result.status == "skipped-symlink" or result.status.startswith("error:")
    assert root.is_symlink()
    assert destination.read_bytes() == original


def test_cleanup_error_is_reported_without_losing_root(
    global_tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed removal is not reported as a successful cleanup."""
    source, config, _ = global_tree
    targets = [KNOWN_TARGETS["claude"]]
    compile_user_root_contexts(targets, source)
    root = config / "CLAUDE.md"
    before = root.read_bytes()
    _native_rules(config)
    original_unlink = Path.unlink

    def blocked_unlink(path: Path, *args, **kwargs) -> None:
        if path == root:
            raise PermissionError("read-only output")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    result = compile_user_root_contexts(targets, source, clean=True)[0]
    assert result.status.startswith("error:")
    assert root.read_bytes() == before


def test_cleanup_preserves_cyclic_root_symlink(global_tree) -> None:
    """An unresolvable root cannot cause a traceback or be deleted."""
    source, config, _ = global_tree
    _native_rules(config)
    root = config / "CLAUDE.md"
    root.symlink_to(root.name)
    result = compile_user_root_contexts([KNOWN_TARGETS["claude"]], source, clean=True)[0]
    assert result.status == "skipped-symlink" or result.status.startswith("error:")
    assert root.is_symlink()
