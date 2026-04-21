# Implementation Plan — PR #700 Architectural Fixes

## Overview

Three targeted refactors requested by review, all scoped inside a single PR.
They share no mutual data-flow dependency, so they can be implemented as three
independent commits, but must be **ordered** to minimise merge-conflict surface.

---

## Change 1: `LockedDependency.to_dependency_ref()` — Eliminate 3 Duplicate Reconstructions

### Problem

Three call sites manually reconstruct a `DependencyReference` from a
`LockedDependency` using an identical 8-field constructor call.  The field
mapping is non-trivial (`registry_prefix` → `artifactory_prefix`;
`source == "local"` → `is_local`), creating a maintenance hazard.

| # | File | Function | Lines |
|---|------|----------|-------|
| 1 | `src/apm_cli/deps/lockfile.py` | `LockFile.get_installed_paths()` | 375-385 |
| 2 | `src/apm_cli/commands/_helpers.py` | `_build_expected_install_paths()` | 138-148 |
| 3 | `src/apm_cli/bundle/plugin_exporter.py` | `_dep_install_path()` | 399-409 |

### Design

Add a new method on `LockedDependency`:

```python
def to_dependency_ref(self) -> "DependencyReference":
    """Reconstruct the DependencyReference needed for path computation."""
    from ..models.apm_package import DependencyReference
    return DependencyReference(
        repo_url=self.repo_url,
        host=self.host,
        virtual_path=self.virtual_path,
        is_virtual=self.is_virtual,
        artifactory_prefix=self.registry_prefix,
        is_local=(self.source == "local"),
        local_path=self.local_path,
        is_insecure=self.is_insecure,
        allow_insecure=self.allow_insecure,
    )
```

Lazy import inside the method body avoids circular dependency
(`lockfile.py` → `reference.py`; `reference.py` does NOT import lockfile).

### Files Changed (4)

| File | Change |
|------|--------|
| `src/apm_cli/deps/lockfile.py` | Add `to_dependency_ref()` to `LockedDependency`; refactor `get_installed_paths()` to call it |
| `src/apm_cli/commands/_helpers.py` | Replace 10-line constructor with `dep.to_dependency_ref()` |
| `src/apm_cli/bundle/plugin_exporter.py` | Replace 10-line constructor with `dep.to_dependency_ref()` |
| `tests/test_lockfile.py` | Add unit test for `to_dependency_ref()` round-trip fidelity |

### Risks

- **Low**: Pure refactor — no behaviour change.  Existing tests for
  `get_installed_paths`, `_build_expected_install_paths`, and
  `_dep_install_path` serve as regression coverage.
- **Field drift**: If a future field is added to the reconstruction,
  only ONE site needs updating.  This is the whole point.

---

## Change 2: Scheme-Blind Identity / Drift Handling

### Problem

`get_unique_key()` (both on `DependencyReference` and `LockedDependency`)
returns `repo_url`, which is scheme-blind: `owner/repo` is the same key
whether the dependency is HTTP or HTTPS.  This means:

1. **Identity collision** — an `http://` dep and an `https://` dep to the same
   `owner/repo` silently overwrite each other in the lockfile dict (keyed by
   `get_unique_key()`).
2. **Drift not detected** — `detect_ref_change()` in `drift.py` compares
   `dep_ref.reference != locked_dep.resolved_ref` but never checks whether
   the *scheme* changed.  The docstring at `drift.py:41-46` explicitly
   documents this as a known non-goal ("Source/host/scheme changes — *not*
   detected").
3. **Silent lockfile corruption on scheme switch** — if a user edits
   `apm.yml` from `http://host/owner/repo` to `https://host/owner/repo`
   (fixing a security issue), the lockfile entry retains `is_insecure: true`
   from the old lock, and `build_download_ref()` (drift.py:253) restores
   `is_insecure=True` to the download ref, defeating the upgrade.

### Design

This is a **policy decision** with two possible depths:

**Option A — Minimal (recommended for this PR):** Make scheme change trigger
re-download by adding a scheme check in `build_download_ref()`:

```python
# In build_download_ref(), after the lockfile lookup:
if locked_dep.is_insecure != dep_ref.is_insecure:
    return dep_ref  # scheme changed → re-download from manifest
```

This ensures that an HTTP→HTTPS (or HTTPS→HTTP) change in `apm.yml` is not
masked by the lockfile replay.  The unique key stays scheme-blind (same
filesystem path), but the *download* respects the manifest's scheme.

**Option B — Full (consider for follow-up):** Include scheme in
`get_unique_key()` so that HTTP and HTTPS versions of the same repo are
truly distinct entries.  This has much wider blast radius (lockfile format,
orphan detection, filesystem layout).

### Files Changed (Option A — 3 files)

| File | Change |
|------|--------|
| `src/apm_cli/drift.py` | Add scheme-change guard in `build_download_ref()`; update module docstring to remove the "not detected" caveat |
| `tests/unit/test_install_update.py` | Add test: HTTP→HTTPS scheme change triggers re-download; add test: HTTPS→HTTP without `allow_insecure` is rejected |
| `tests/unit/test_install_update.py` | Verify existing `test_http_lockfile_restores_insecure_scheme` still passes (no change to it, just run) |

### Risks

- **Medium — lockfile replay**: If an existing lockfile has `is_insecure: true`
  and the user switches to HTTPS in `apm.yml`, the dep will be re-downloaded
  on next install.  This is *correct* but is a behavioural change for users
  who previously edited scheme without re-downloading.
- **Low — identity stability**: `get_unique_key()` is unchanged, so lockfile
  keying, orphan detection, and filesystem layout are unaffected.
- **None — `get_identity()`**: Already scheme-blind by design (it serves
  duplicate detection, not lockfile keying).  No change needed.

---

## Change 3: Move Insecure Policy Logic to Install Module

### Problem

10 insecure-policy functions and 1 dataclass live in
`src/apm_cli/commands/install.py` (lines 111-306).  The resolve phase
(`install/phases/resolve.py:300-305`) imports them back:

```python
from apm_cli.commands.install import (
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,
    _guard_transitive_insecure_dependencies,
    _warn_insecure_dependencies,
)
```

This creates a **circular dependency smell** (`commands/ → install/` and
`install/ → commands/`) and places pure-domain logic inside a Click CLI module.

### Design

Create `src/apm_cli/install/insecure_policy.py` containing:

- `_InsecureDependencyInfo` (dataclass)
- `_collect_insecure_dependency_infos()`
- `_format_insecure_dependency_warning()`
- `_warn_insecure_dependencies()`
- `_normalize_allow_insecure_host()`
- `_allow_insecure_host_callback()` (Click callback — needs `click` import)
- `_get_insecure_dependency_host()`
- `_get_allowed_transitive_insecure_hosts()`
- `_guard_transitive_insecure_dependencies()`
- `_check_insecure_dependencies()`

In `commands/install.py`, add re-exports to preserve the existing test import
contract:

```python
from apm_cli.install.insecure_policy import (
    _InsecureDependencyInfo,
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,
    _format_insecure_dependency_warning,
    _guard_transitive_insecure_dependencies,
    _warn_insecure_dependencies,
    _allow_insecure_host_callback,
    _normalize_allow_insecure_host,
    _get_insecure_dependency_host,
    _get_allowed_transitive_insecure_hosts,
)
```

The resolve phase import changes to the canonical location:

```python
from apm_cli.install.insecure_policy import (
    _check_insecure_dependencies,
    _collect_insecure_dependency_infos,
    _guard_transitive_insecure_dependencies,
    _warn_insecure_dependencies,
)
```

### Files Changed (4 + 1 new)

| File | Change |
|------|--------|
| `src/apm_cli/install/insecure_policy.py` | **NEW** — all 10 functions + dataclass moved here |
| `src/apm_cli/commands/install.py` | Delete function bodies (lines 111-306); add re-export block |
| `src/apm_cli/install/phases/resolve.py` | Update import from `apm_cli.install.insecure_policy` |
| `tests/unit/test_install_command.py` | No change needed if re-exports preserved.  Optionally add a parallel import path test. |
| `tests/unit/install/test_insecure_policy.py` | **NEW** (optional) — dedicated tests for the new module, migrated from test_install_command.py |

### Risks

- **Low — import compat**: Re-exports from `commands/install.py` keep all 17
  existing test patches (e.g. `@patch("apm_cli.commands.install._check_insecure_dependencies")`)
  working.  This follows the precedent already set in `install.py` lines 34-70
  for `_hash_deployed`, `_validate_package_exists`, `_pre_deploy_security_scan`, etc.
- **Low — Click dependency**: `_allow_insecure_host_callback` needs `click`;
  the install package already imports click transitively (via context/request).
  Keep the import local to that one function.
- **None — runtime**: Pure file-move.  No logic change.

---

## Recommended Commit Order

| Order | Change | Rationale |
|-------|--------|-----------|
| **1** | `to_dependency_ref()` | Smallest blast radius (4 files). Self-contained refactor. No test import changes. Easiest to review. |
| **2** | Scheme-blind drift fix | Builds on stable lockfile model from (1). Adds a single guard in `drift.py`. Small, focused. |
| **3** | Move insecure policy | Largest file-move. Touches the most test-adjacent surface. Doing it last means reviewers already understand the insecure plumbing from (2). |

Each commit should be independently green (all tests pass). This ordering
minimises rebase conflicts if any commit needs rework.

---

## Test Strategy

| Commit | New Tests | Existing Coverage |
|--------|-----------|-------------------|
| 1 | 1 unit test for `to_dependency_ref()` round-trip | `test_lockfile.py` (44 tests), `test_transitive_deps.py` |
| 2 | 2 tests for scheme-change detection | `test_install_update.py` (existing HTTP lockfile test) |
| 3 | Import-path smoke test (optional) | All 17 tests in `TestAllowInsecureFlag`, `TestInsecureDependencyWarnings`, `TestTransitiveInsecureDependencyGuard` pass unchanged via re-exports |

Total new test count: ~3-4 tests.
Total files touched: 8 (+ 1-2 new).