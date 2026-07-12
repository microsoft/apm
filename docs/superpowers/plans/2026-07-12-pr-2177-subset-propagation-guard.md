# PR 2177 Subset Propagation Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make audit replay's locked skill-subset propagation a statically enforced architecture contract.

**Architecture:** `LockedDependency.to_dependency_ref()` reconstructs the persisted fact and `run_replay()` forwards it without reinterpretation. The architecture lint and an AST-based integration assertion protect both edges.

**Tech Stack:** Bash, Python `ast`, pytest, uv, git

---

### Task 1: Add the failing architecture assertion

**Files:**
- Modify: `tests/integration/test_architecture_intent_guards.py`

- [ ] **Step 1: Add AST helpers and the failing test**

```python
import ast


def _find_call_keyword(
    source: str,
    *,
    owner: str,
    call_name: str,
    keyword_name: str,
) -> ast.expr:
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == owner
    )
    call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == call_name
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == call_name
        )
    )
    return next(item.value for item in call.keywords if item.arg == keyword_name)


def test_audit_replay_preserves_locked_skill_subset_authority() -> None:
    root = Path(__file__).parents[2]
    lockfile = (root / "src/apm_cli/deps/lockfile.py").read_text()
    drift = (root / "src/apm_cli/install/drift.py").read_text()
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    reconstructed = _find_call_keyword(
        lockfile,
        owner="to_dependency_ref",
        call_name="DependencyReference",
        keyword_name="skill_subset",
    )
    replayed = _find_call_keyword(
        drift,
        owner="run_replay",
        call_name="integrate_package_primitives",
        keyword_name="skill_subset",
    )

    assert "self.skill_subset" in ast.unparse(reconstructed)
    assert "package_info.dependency_ref.skill_subset" in ast.unparse(replayed)
    assert "Audit replay must preserve locked skill subset intent" in guard
```

- [ ] **Step 2: Run the test and verify the missing guard fails**

Run:

```bash
uv run --extra dev pytest \
  tests/integration/test_architecture_intent_guards.py::test_audit_replay_preserves_locked_skill_subset_authority \
  -x
```

Expected: FAIL because the boundary label is absent.

### Task 2: Add the AC4 boundary guard

**Files:**
- Modify: `scripts/lint-architecture-boundaries.sh`

- [ ] **Step 1: Add positive owner-routing checks under AC4**

```bash
if ! grep -A45 'def to_dependency_ref' src/apm_cli/deps/lockfile.py \
    | grep -q 'skill_subset=sorted(self.skill_subset)'; then
    echo "[x] Audit replay must preserve locked skill subset intent"
    violations=$((violations + 1))
fi
if ! grep -A40 'integrate_package_primitives(' src/apm_cli/install/drift.py \
    | grep -q 'skill_subset=tuple(package_info.dependency_ref.skill_subset or ()) or None'; then
    echo "[x] Audit replay must preserve locked skill subset intent"
    violations=$((violations + 1))
fi
```

- [ ] **Step 2: Run the focused tests and lint**

```bash
bash scripts/lint-architecture-boundaries.sh
uv run --extra dev pytest \
  tests/integration/test_architecture_intent_guards.py \
  tests/unit/install/test_drift.py \
  tests/integration/test_drift_check.py \
  -q
```

Expected: boundary lint clean; all tests pass.

- [ ] **Step 3: Run both mutation probes**

Temporarily remove the `skill_subset=` argument from
`LockedDependency.to_dependency_ref()`, then run the architecture and drift
tests. Restore it. Temporarily replace replay's argument with
`skill_subset=None`, rerun the same tests, and restore it.

Expected: each mutation fails at least one behavioral test and the architecture
test; the boundary lint fails in both cases.

- [ ] **Step 4: Commit and push to the existing PR branch**

```bash
git add scripts/lint-architecture-boundaries.sh \
  tests/integration/test_architecture_intent_guards.py
git commit -m "test(audit): guard locked subset replay authority" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>" \
  -m "Copilot-Session: 7955c89b-a997-42aa-9c45-ef4c7fe4b1e7"
git push origin HEAD:fix/audit-skill-subset-2172
```

