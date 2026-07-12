"""Source invariants for the canonical target capability owner."""

import ast
from pathlib import Path

TARGET_CATALOG = Path("src/apm_cli/core/target_catalog.py")
SOURCE_ROOT = Path("src/apm_cli")
TARGET_NAMES = frozenset(
    {
        "agent-skills",
        "antigravity",
        "claude",
        "codex",
        "copilot",
        "copilot-app",
        "copilot-cowork",
        "cursor",
        "gemini",
        "hermes",
        "intellij",
        "kiro",
        "openclaw",
        "opencode",
        "vscode",
        "windsurf",
    }
)
COMMAND_NAMES = frozenset({"compile", "install", "update"})
CAPABILITY_WORDS = ("capabil", "command", "compil")


def _string_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _foreign_capability_definitions(tree: ast.AST) -> list[tuple[int, str]]:
    """Find target-to-command capability data declared outside its owner."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue

        names = _assigned_names(node)
        lowered_names = tuple(name.lower() for name in names)
        literals = _string_literals(node.value)
        target_literals = literals & TARGET_NAMES
        command_literals = literals & COMMAND_NAMES

        if isinstance(node.value, ast.Dict):
            key_targets = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in TARGET_NAMES
            }
            capability_values = [
                value
                for key, value in zip(node.value.keys, node.value.values, strict=True)
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in TARGET_NAMES
                and isinstance(value, (ast.Dict, ast.Set, ast.List, ast.Tuple))
                and bool(_string_literals(value) & (COMMAND_NAMES | {"commands", "compilable"}))
            ]
            if len(key_targets) >= 2 and len(capability_values) >= 2:
                findings.append((node.lineno, names[0] if names else "<assignment>"))
                continue

        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "dict"
        ):
            capability_keywords = [
                keyword
                for keyword in node.value.keywords
                if keyword.arg is not None
                and keyword.arg.replace("_", "-") in TARGET_NAMES
                and isinstance(keyword.value, (ast.Dict, ast.Set, ast.List, ast.Tuple))
                and bool(
                    _string_literals(keyword.value) & (COMMAND_NAMES | {"commands", "compilable"})
                )
            ]
            if len(capability_keywords) >= 2:
                findings.append((node.lineno, names[0] if names else "<assignment>"))
                continue

        capability_named = any(
            "target" in name
            and any(word in name for word in CAPABILITY_WORDS)
            and "family" not in name
            for name in lowered_names
        )
        if capability_named and len(target_literals) >= 2:
            findings.append((node.lineno, names[0] if names else "<assignment>"))
            continue

        per_target_commands = any(
            "command" in name and any(target.replace("-", "_") in name for target in TARGET_NAMES)
            for name in lowered_names
        )
        if per_target_commands and command_literals:
            findings.append((node.lineno, names[0] if names else "<assignment>"))

    return findings


def test_foreign_capability_map_fixture_is_detected() -> None:
    """Prove a short duplicate capability map cannot evade the guard."""
    tree = ast.parse(
        """
TARGET_SUPPORT = {
    "copilot": {"compile": True, "install": True},
    "intellij": {"compile": False, "install": True},
}
"""
    )

    assert _foreign_capability_definitions(tree) == [(2, "TARGET_SUPPORT")]


def test_alias_and_dict_constructor_capability_maps_are_detected() -> None:
    """Alternate literal spellings cannot bypass the capability owner."""
    alias_tree = ast.parse(
        """
COMPILE_TARGETS = {"vscode", "cursor"}
"""
    )
    constructor_tree = ast.parse(
        """
TARGET_CAPABILITIES = dict(
    copilot={"compile", "install"},
    intellij={"install"},
)
"""
    )

    assert _foreign_capability_definitions(alias_tree) == [(2, "COMPILE_TARGETS")]
    assert _foreign_capability_definitions(constructor_tree) == [(2, "TARGET_CAPABILITIES")]


def test_target_catalog_is_only_per_target_command_capability_owner() -> None:
    """No Python module outside target_catalog may redeclare command support."""
    findings: list[str] = []
    for source_path in SOURCE_ROOT.rglob("*.py"):
        if source_path == TARGET_CATALOG:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        findings.extend(
            f"{source_path}:{line} ({name})" for line, name in _foreign_capability_definitions(tree)
        )

    assert findings == []
