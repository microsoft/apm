# PR 2094 Windows Stable Path Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declare and enforce `install.ps1` as the sole production owner of the stable Windows executable path.

**Architecture:** The installer computes `$installRoot/current/apm.exe`; tests validate it but do not become production owners. Architecture lint rejects a second production derivation and confirms the owner contract remains intact.

**Tech Stack:** PowerShell source, Bash, Python pytest, uv, git

---

### Task 1: Add the failing architecture contract

**Files:**
- Modify: `.github/instructions/architecture.instructions.md`
- Modify: `tests/integration/test_architecture_authorities.py`

- [ ] **Step 1: Add the owner row**

```markdown
| Windows stable executable path | install.ps1 (`$currentDir` / `$currentExe`) |
```

- [ ] **Step 2: Add the architecture test**

```python
def test_windows_stable_executable_has_one_production_owner() -> None:
    """Only install.ps1 may derive the stable current/apm.exe path."""
    root = Path(__file__).parents[2]
    installer = (root / "install.ps1").read_text(encoding="utf-8")
    guard = (root / "scripts/lint-architecture-boundaries.sh").read_text()

    assert '$currentDir = Join-Path $installRoot "current"' in installer
    assert '$currentExe = Join-Path $currentDir "apm.exe"' in installer
    assert "Add-ToUserPath -PathEntry $currentDir" in installer
    assert "Windows stable executable path belongs to install.ps1" in guard
```

- [ ] **Step 3: Run the test and verify failure**

```bash
uv run --extra dev pytest \
  tests/integration/test_architecture_authorities.py::test_windows_stable_executable_has_one_production_owner \
  -x
```

Expected: FAIL because the guard label is missing.

### Task 2: Add owner and duplicate checks

**Files:**
- Modify: `scripts/lint-architecture-boundaries.sh`

- [ ] **Step 1: Add owner-presence checks under AC4**

```bash
if ! grep -Fq '$currentDir = Join-Path $installRoot "current"' install.ps1 \
    || ! grep -Fq '$currentExe = Join-Path $currentDir "apm.exe"' install.ps1 \
    || ! grep -Fq 'Add-ToUserPath -PathEntry $currentDir' install.ps1; then
    echo "[x] Windows stable executable path belongs to install.ps1"
    violations=$((violations + 1))
fi
```

- [ ] **Step 2: Reject production duplicates**

```bash
windows_stable_path_hits=$(
    grep -rEn 'current[/\\]+apm\.exe|Join-Path .*["'\'']current["'\'']' \
        src/apm_cli .github/workflows scripts/windows \
        --include='*.py' --include='*.ps1' --include='*.yml' --include='*.yaml' \
        | grep -v 'scripts/windows/test-install-script.ps1' \
        | grep -v 'architecture-authority-exempt:' \
        || true
)
if [ -n "$windows_stable_path_hits" ]; then
    echo "[x] Windows stable executable path belongs to install.ps1"
    echo "$windows_stable_path_hits"
    violations=$((violations + 1))
fi
```

- [ ] **Step 3: Run focused tests**

```bash
bash scripts/lint-architecture-boundaries.sh
uv run --extra dev pytest \
  tests/integration/test_architecture_authorities.py \
  tests/unit/test_windows_installer_launchers.py \
  -q
```

Expected: clean lint; all tests pass.

- [ ] **Step 4: Mutation-break the production allowlist**

Create `scripts/windows/stable-path-probe.ps1` containing:

```powershell
$StableExe = Join-Path $InstallRoot "current\apm.exe"
```

Run the boundary lint and verify it fails with the Windows owner label. Delete
the probe and rerun the lint to green.

- [ ] **Step 5: Commit and push**

```bash
git add .github/instructions/architecture.instructions.md \
  scripts/lint-architecture-boundaries.sh \
  tests/integration/test_architecture_authorities.py
git commit -m "test(windows): guard stable executable owner" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>" \
  -m "Copilot-Session: 7955c89b-a997-42aa-9c45-ef4c7fe4b1e7"
git push origin HEAD:fix/windows-apm-exe-shim
```

