# PR 2176 Subset Owner Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the skill-subset owner guard reject renamed local normalizers in install and pack consumers.

**Architecture:** A focused AST checker owns detection of the normalization shape. The Bash architecture lint invokes it, and unit plus integration tests verify both the checker and its wiring.

**Tech Stack:** Python `ast`, Bash, pytest, uv, git

---

### Task 1: Specify renamed-normalizer detection

**Files:**
- Create: `tests/unit/scripts/test_check_skill_subset_authority.py`
- Create: `scripts/check_skill_subset_authority.py`

- [ ] **Step 1: Write the failing checker tests**

```python
from pathlib import Path

from scripts.check_skill_subset_authority import find_local_normalizers


def test_renamed_subset_normalizer_is_rejected(tmp_path: Path) -> None:
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        """
from pathlib import PurePosixPath

def promotion_tokens(names):
    tokens = set()
    for value in names:
        normalized = str(value).replace("\\\\", "/")
        tokens.add(PurePosixPath(normalized).name)
    return tokens
""",
        encoding="utf-8",
    )

    assert find_local_normalizers([consumer]) == ["consumer.py:promotion_tokens"]


def test_canonical_owner_call_is_allowed(tmp_path: Path) -> None:
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "tokens = skill_subset_filter_tokens(skill_subset)\n",
        encoding="utf-8",
    )

    assert find_local_normalizers([consumer]) == []
```

- [ ] **Step 2: Run the tests and verify import failure**

```bash
uv run --extra dev pytest tests/unit/scripts/test_check_skill_subset_authority.py -x
```

Expected: FAIL because the checker does not exist.

- [ ] **Step 3: Implement the focused AST checker**

```python
#!/usr/bin/env python3
"""Reject local skill-subset token normalizers outside their owner."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _normalizes_slashes(node: ast.Call) -> bool:
    if _call_name(node) != "replace" or len(node.args) < 2:
        return False
    values = [
        arg.value
        for arg in node.args[:2]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    return values == ["\\", "/"]


def _is_local_normalizer(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    has_path_leaf = any(_call_name(node) in {"Path", "PurePath", "PurePosixPath"} for node in calls) and any(
        isinstance(node, ast.Attribute) and node.attr == "name"
        for node in ast.walk(function)
    )
    has_slash_normalization = any(_normalizes_slashes(node) for node in calls)
    has_token_collection = any(_call_name(node) in {"add", "update"} for node in calls)
    return has_path_leaf and has_slash_normalization and has_token_collection


def find_local_normalizers(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_local_normalizer(node):
                offenders.append(f"{path.name}:{node.name}")
    return sorted(offenders)


def main(argv: list[str]) -> int:
    offenders = find_local_normalizers([Path(value) for value in argv])
    for offender in offenders:
        print(offender)
    return int(bool(offenders))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run the checker tests**

```bash
uv run --extra dev pytest tests/unit/scripts/test_check_skill_subset_authority.py -q
```

Expected: 2 passed.

### Task 2: Wire the checker into the architecture boundary

**Files:**
- Modify: `scripts/lint-architecture-boundaries.sh`
- Modify: `tests/integration/test_architecture_authorities.py`

- [ ] **Step 1: Replace the narrow subset guard with checker invocation**

```bash
if ! python scripts/check_skill_subset_authority.py \
    src/apm_cli/integration/skill_integrator.py \
    src/apm_cli/bundle/plugin_exporter.py; then
    echo "[x] Skill subset filter tokens must come from models/dependency/subsets.py"
    violations=$((violations + 1))
fi
```

Keep the existing lexical check for retired symbols as a cheap first line.

- [ ] **Step 2: Extend the architecture assertion**

Add imports for `subprocess` and `sys`, then add:

```python
result = subprocess.run(
    [
        sys.executable,
        str(root / "scripts/check_skill_subset_authority.py"),
        str(root / "src/apm_cli/integration/skill_integrator.py"),
        str(root / "src/apm_cli/bundle/plugin_exporter.py"),
    ],
    check=False,
    capture_output=True,
    text=True,
)
assert result.returncode == 0, result.stdout + result.stderr
assert "check_skill_subset_authority.py" in guard
```

- [ ] **Step 3: Run focused validation**

```bash
bash scripts/lint-architecture-boundaries.sh
uv run --extra dev pytest \
  tests/unit/scripts/test_check_skill_subset_authority.py \
  tests/integration/test_architecture_authorities.py \
  tests/unit/test_plugin_exporter.py \
  tests/unit/integration/test_skill_integrator.py \
  -q
```

Expected: clean lint; all tests pass.

- [ ] **Step 4: Prove the renamed-helper mutation is caught**

Add the renamed `promotion_tokens()` fixture implementation to either consumer,
run the boundary lint and architecture test, verify both fail, then restore the
consumer.

- [ ] **Step 5: Commit and push**

```bash
git add scripts/check_skill_subset_authority.py \
  scripts/lint-architecture-boundaries.sh \
  tests/unit/scripts/test_check_skill_subset_authority.py \
  tests/integration/test_architecture_authorities.py
git commit -m "test(deps): harden subset owner boundary" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>" \
  -m "Copilot-Session: 7955c89b-a997-42aa-9c45-ef4c7fe4b1e7"
git push origin HEAD:fix/2171-prefixed-skills
```

