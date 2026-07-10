# Plugin Pack Source Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revise PR #2122 so `apm pack` prevents accidental root-folder
publication after `.apm/` adoption without breaking native Claude plugins that
author in root convention directories.

**Architecture:** Separate source-layout detection from publication consent.
The project-root `.apm/` directory selects APM-native layout; its absence
selects plugin-native root discovery. Explicit `includes` lists remain the
strongest publication boundary. Mixed layouts pack from `.apm/` and emit
actionable warnings for skipped root directories.

**Tech Stack:** Python 3.11+, Click, pytest, subprocess-based integration tests,
Starlight Markdown.

**Design source:** Commit `c6c3c713d`, file
`docs/superpowers/specs/2026-07-10-plugin-pack-source-layout-design.md`.

---

## File Map

- Modify `src/apm_cli/bundle/plugin_exporter.py`: select local source layout,
  warn on skipped mixed-layout directories, and retain dependency discovery.
- Create `src/apm_cli/bundle/plugin_layout.py`: hold the shared native-root
  source names and detect which ones are present.
- Modify `src/apm_cli/commands/_helpers.py`: explain native-plugin discovery
  during `apm init` without creating `.apm/`.
- Modify `tests/unit/test_plugin_exporter.py`: pin the layout matrix and warning
  contract, including root hook configuration.
- Modify `tests/unit/test_init_command.py`: pin init messaging for native
  convention directories.
- Modify `tests/integration/test_pack_root_skills_e2e.py`: cover native
  Claude-plugin init-to-pack behavior and `.apm/` authority.
- Modify `docs/src/content/docs/producer/pack-a-bundle.md`: document the source
  switch and mixed-layout warning.
- Modify `docs/src/content/docs/producer/repo-shapes.md`: document progressive
  migration.
- Modify `docs/src/content/docs/reference/cli/pack.md`: document pack behavior.
- Modify `docs/src/content/docs/reference/manifest-schema.md`: keep `includes`
  consent separate from layout.
- Modify `docs/src/content/docs/getting-started/first-package.md`: preserve the
  plugin-native onboarding path.
- Modify `packages/apm-guide/.apm/skills/apm-usage/package-authoring.md`: align
  agent-facing authoring guidance.
- Modify `CHANGELOG.md`: replace PR #2122's current discriminator claim with the
  source-layout decision.

### Task 1: Reground PR #2122 and run the design panel

**Files:**
- Read: `docs/superpowers/specs/2026-07-10-plugin-pack-source-layout-design.md`
- Read: `src/apm_cli/bundle/plugin_exporter.py`
- Read: `src/apm_cli/commands/_helpers.py`
- Read: `tests/integration/test_pack_root_skills_e2e.py`

- [ ] **Step 1: Check out and rebase the PR head**

```bash
gh pr checkout 2122 --repo microsoft/apm
git fetch origin main
git rebase origin/main
```

Expected: the branch is based on current `main`; resolve only faithful conflicts
within PR #2122 files.

- [ ] **Step 2: Load the approved design**

```bash
git show c6c3c713d:docs/superpowers/specs/2026-07-10-plugin-pack-source-layout-design.md
```

Expected: the decision says `.apm/` presence selects APM-native layout,
plugin-native roots remain sources when `.apm/` is absent, and mixed layouts
warn.

- [ ] **Step 3: Run `apm-review-panel` with the design as mandatory context**

Require the panel to answer:

1. Does `.apm/` presence provide a deterministic authority boundary?
2. Does the proposal preserve official Claude plugin-root conventions?
3. Are explicit `includes` paths still treated as the strongest publication
   boundary?
4. Are warnings actionable and ASCII-safe?

Expected: fold every in-scope panel finding into PR #2122 before terminal
`ship_now`.

### Task 2: Pin source-layout selection with unit tests

**Files:**
- Modify: `tests/unit/test_plugin_exporter.py`
- Test: `tests/unit/test_plugin_exporter.py`

- [ ] **Step 1: Replace the PR's includes-based unit test**

Replace `test_declared_includes_excludes_root_level_plugin_dirs` with tests that
make `.apm/` presence the only implicit-layout discriminator:

```python
def test_apm_dir_excludes_root_level_plugin_dirs(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    _write_apm_yml(project, extra={"includes": "auto"})
    (project / ".apm").mkdir(exist_ok=True)
    root_agents = project / "agents"
    root_agents.mkdir()
    (root_agents / "root-bot.agent.md").write_text("root bot", encoding="utf-8")

    result = export_plugin_bundle(project, tmp_path / "build")

    assert not (result.bundle_path / "agents" / "root-bot.agent.md").exists()


def test_auto_includes_preserves_native_root_without_apm_dir(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    _write_apm_yml(project, extra={"includes": "auto"})
    root_agents = project / "agents"
    root_agents.mkdir()
    (root_agents / "root-bot.agent.md").write_text("root bot", encoding="utf-8")

    result = export_plugin_bundle(project, tmp_path / "build")

    assert (result.bundle_path / "agents" / "root-bot.agent.md").is_file()
```

- [ ] **Step 2: Add omitted-includes matrix coverage**

```python
def test_omitted_includes_with_apm_dir_skips_root_components(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    (project / ".apm").mkdir(exist_ok=True)
    root_skills = project / "skills" / "root-skill"
    root_skills.mkdir(parents=True)
    (root_skills / "SKILL.md").write_text("# Root\n", encoding="utf-8")

    result = export_plugin_bundle(project, tmp_path / "build")

    assert not (result.bundle_path / "skills" / "root-skill").exists()
```

- [ ] **Step 3: Add explicit-includes and root-hooks coverage**

```python
def test_explicit_includes_does_not_restore_root_components(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    _write_apm_yml(project, extra={"includes": [".apm/agents/published.agent.md"]})
    agents = project / ".apm" / "agents"
    agents.mkdir(parents=True)
    (agents / "published.agent.md").write_text("published", encoding="utf-8")
    root_agents = project / "agents"
    root_agents.mkdir()
    (root_agents / "draft.agent.md").write_text("draft", encoding="utf-8")

    result = export_plugin_bundle(project, tmp_path / "build")

    assert (result.bundle_path / "agents" / "published.agent.md").is_file()
    assert not (result.bundle_path / "agents" / "draft.agent.md").exists()


def test_explicit_includes_are_exhaustive(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    _write_apm_yml(project, extra={"includes": [".apm/agents/published.agent.md"]})
    agents = project / ".apm" / "agents"
    agents.mkdir(parents=True)
    (agents / "published.agent.md").write_text("published", encoding="utf-8")
    (agents / "private.agent.md").write_text("private", encoding="utf-8")

    result = export_plugin_bundle(project, tmp_path / "build")

    assert (result.bundle_path / "agents" / "published.agent.md").is_file()
    assert not (result.bundle_path / "agents" / "private.agent.md").exists()


def test_missing_explicit_include_fails_pack(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    _write_apm_yml(project, extra={"includes": [".apm/agents/missing.agent.md"]})

    with pytest.raises(
        ValueError,
        match=r"includes path '\.apm/agents/missing\.agent\.md' does not exist",
    ):
        export_plugin_bundle(project, tmp_path / "build")


def test_apm_dir_excludes_root_hook_config(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    apm_hooks = project / ".apm" / "hooks"
    apm_hooks.mkdir(parents=True)
    (apm_hooks / "hooks.json").write_text(
        json.dumps({"preCommit": ["published"]}),
        encoding="utf-8",
    )
    (project / "hooks.json").write_text(
        json.dumps({"postPush": ["draft"]}),
        encoding="utf-8",
    )

    result = export_plugin_bundle(project, tmp_path / "build")
    hooks = json.loads((result.bundle_path / "hooks.json").read_text(encoding="utf-8"))

    assert hooks == {"preCommit": ["published"]}


def test_apm_authority_preserves_dependency_components(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    (project / ".apm").mkdir(exist_ok=True)
    deployed = _write_deployed_agent(project, "dep-agent.agent.md", "dependency")
    dep = LockedDependency(
        repo_url="acme/tools",
        depth=1,
        deployed_files=deployed,
    )
    _write_lockfile(project, [dep])

    result = export_plugin_bundle(project, tmp_path / "build")

    assert (result.bundle_path / "agents" / "dep-agent.agent.md").is_file()


def test_empty_apm_dir_warns_when_no_local_primitives_exist(self, tmp_path):
    project = _setup_plugin_project(tmp_path)
    (project / ".apm").mkdir(exist_ok=True)
    captured = []

    class _StubLogger:
        def warning(self, message):
            captured.append(message)

    export_plugin_bundle(project, tmp_path / "build", logger=_StubLogger())

    assert captured == [
        "No local primitives found. Expected content under .apm/. "
        "Check the project layout or move plugin-native content into .apm/."
    ]
```

- [ ] **Step 4: Add mixed-layout warning coverage**

Use the `_StubLogger` pattern already present in this module. Assert exact
semantics rather than ANSI formatting:

```python
captured = []


class _StubLogger:
    def warning(self, message):
        captured.append(message)


result = export_plugin_bundle(
    project,
    tmp_path / "build",
    logger=_StubLogger(),
)
assert result.bundle_path.is_dir()
assert captured == [
    "Skipping root-level agents/ because .apm/ is present. "
    "Move publishable files to .apm/agents/ or remove agents/ "
    "to silence this warning.",
    "Skipping root-level hooks.json because .apm/ is present. "
    "Move publishable hook configuration to .apm/hooks/ or remove hooks.json "
    "to silence this warning.",
]
```

- [ ] **Step 5: Run the focused unit tests and confirm RED**

```bash
uv run --extra dev pytest \
  tests/unit/test_plugin_exporter.py::TestExportPluginBundle::test_apm_dir_excludes_root_level_plugin_dirs \
  tests/unit/test_plugin_exporter.py::TestExportPluginBundle::test_auto_includes_preserves_native_root_without_apm_dir \
  tests/unit/test_plugin_exporter.py::TestExportPluginBundle::test_omitted_includes_with_apm_dir_skips_root_components \
  tests/unit/test_plugin_exporter.py::TestExportPluginBundle::test_apm_dir_excludes_root_hook_config \
  -xvs
```

Expected: at least the native-root and omitted-includes cases fail against PR
#2122's `has_declared_includes` gate.

### Task 3: Implement deterministic local source selection

**Files:**
- Create: `src/apm_cli/bundle/plugin_layout.py`
- Modify: `src/apm_cli/bundle/plugin_exporter.py:121-130`
- Modify: `src/apm_cli/bundle/plugin_exporter.py:768-778`
- Test: `tests/unit/test_plugin_exporter.py`

- [ ] **Step 1: Add shared source detection**

```python
"""Plugin-native source-layout conventions."""

from pathlib import Path

PLUGIN_ROOT_DIRS = ("agents", "skills", "commands", "instructions", "extensions", "hooks")


def find_plugin_root_sources(project_root: Path) -> list[str]:
    """Return plugin-native root sources that exist."""
    sources = [name for name in PLUGIN_ROOT_DIRS if (project_root / name).is_dir()]
    if (project_root / "hooks.json").is_file():
        sources.append("hooks.json")
    return sources
```

- [ ] **Step 2: Add a warning emitter**

Add a focused helper next to `_collect_root_plugin_components`:

```python
def _warn_skipped_root_components(
    project_root: Path,
    logger=None,
) -> None:
    """Explain why plugin-native root directories are not packed."""
    for source in find_plugin_root_sources(project_root):
        if source == "hooks.json":
            message = (
                "Skipping root-level hooks.json because .apm/ is present. "
                "Move publishable hook configuration to .apm/hooks/ or remove "
                "hooks.json to silence this warning."
            )
        else:
            message = (
                f"Skipping root-level {source}/ because .apm/ is present. "
                f"Move publishable files to .apm/{source}/ or remove {source}/ "
                "to silence this warning."
            )
        if logger:
            logger.warning(message)
        else:
            _rich_warning(message)


def _warn_no_local_primitives(logger=None) -> None:
    message = (
        "No local primitives found. Expected content under .apm/. "
        "Check the project layout or move plugin-native content into .apm/."
    )
    if logger:
        logger.warning(message)
    else:
        _rich_warning(message)
```

Keep `logger` unannotated, matching `export_plugin_bundle`; do not introduce
`Any` or a new logging abstraction in this bug fix.

- [ ] **Step 3: Add explicit include collection**

Add a local collector that rejects unsafe or missing paths, expands declared
directories recursively, and maps each selected file through
`_plugin_rel_for_deployed_path`:

```python
def _collect_explicit_local_components(
    project_root: Path,
    includes: list[str],
) -> tuple[list[tuple[Path, str]], dict]:
    components: list[tuple[Path, str]] = []
    hooks: dict = {}
    for declared_path in includes:
        parts = _deployed_path_parts(declared_path)
        source = ensure_path_within(project_root.joinpath(*parts), project_root)
        if not source.exists():
            raise ValueError(f"includes path {declared_path!r} does not exist.")
        files = (
            [source]
            if source.is_file()
            else sorted(path for path in source.rglob("*") if path.is_file())
        )
        for file_path in files:
            if file_path.is_symlink():
                raise ValueError(f"Explicit include path is a symlink: {file_path}")
            file_path = ensure_path_within(file_path, project_root)
            repo_relative = portable_relpath(file_path, project_root)
            plugin_relative = _plugin_rel_for_deployed_path(repo_relative, None)
            if plugin_relative is None:
                raise ValueError(
                    f"Explicit include path is not a packable primitive: {repo_relative}"
                )
            if plugin_relative == "hooks.json" or plugin_relative.startswith("hooks/"):
                try:
                    hook_data = json.loads(file_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, RecursionError) as exc:
                    raise ValueError(
                        f"Explicit hook include is not valid JSON: {repo_relative}"
                    ) from exc
                if not isinstance(hook_data, dict):
                    raise ValueError(
                        f"Explicit hook include must contain a JSON object: {repo_relative}"
                    )
                _deep_merge(hooks, hook_data, overwrite=False)
            else:
                components.append((file_path, plugin_relative))
    return components, hooks
```

Import `portable_relpath` from `apm_cli.utils.paths`.

- [ ] **Step 4: Replace the includes gate with the authority algorithm**

```python
own_apm_dir = project_root / ".apm"
if isinstance(package.includes, list):
    own_components, root_hooks = _collect_explicit_local_components(
        project_root,
        package.includes,
    )
else:
    own_components = _collect_apm_components(own_apm_dir)
    root_hooks = _collect_hooks_from_apm(own_apm_dir)
    root_components = _collect_root_plugin_components(project_root)
    if own_apm_dir.is_dir():
        _warn_skipped_root_components(project_root, logger)
    else:
        own_components.extend(root_components)
        root_hooks_top = _collect_hooks_from_root(project_root)
        _deep_merge(root_hooks, root_hooks_top, overwrite=False)

if own_apm_dir.is_dir() and not own_components and not root_hooks:
    _warn_no_local_primitives(logger)
_merge_file_map(file_map, own_components, pkg_name, force, collisions)
_deep_merge(merged_hooks, root_hooks, overwrite=True)
```

Add `.apm/hooks/published.json` and `.apm/hooks/private.json` to the
explicit-list unit fixture and assert only the selected hook key reaches
`hooks.json`.

Do not alter `_collect_deployed_components`: dependency packages retain their
own source layouts.

- [ ] **Step 5: Run focused tests and confirm GREEN**

```bash
uv run --extra dev pytest tests/unit/test_plugin_exporter.py -q
```

Expected: all plugin exporter tests pass.

- [ ] **Step 6: Prove the mutation break**

Temporarily change `if own_apm_dir.is_dir():` to `if False:` and run:

```bash
uv run --extra dev pytest \
  tests/unit/test_plugin_exporter.py::TestExportPluginBundle::test_apm_dir_excludes_root_level_plugin_dirs \
  -q
```

Expected: FAIL because the root agent is packed. Restore the guard and rerun to
PASS.

- [ ] **Step 7: Commit the source-selection slice**

```bash
git add src/apm_cli/bundle/plugin_layout.py src/apm_cli/bundle/plugin_exporter.py tests/unit/test_plugin_exporter.py
git commit -m "fix(pack): select source layout from .apm presence" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: Add native-plugin init-to-pack e2e proof

**Files:**
- Modify: `tests/integration/test_pack_root_skills_e2e.py`
- Test: `tests/integration/test_pack_root_skills_e2e.py`

- [ ] **Step 1: Add a subprocess helper**

```python
def _run_apm(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    apm_executable = Path(sys.executable).with_name("apm")
    return subprocess.run(
        [str(apm_executable), *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
```

- [ ] **Step 2: Add the native Claude plugin journey**

```python
def test_init_then_pack_preserves_native_claude_skill(tmp_path: Path) -> None:
    project = tmp_path / "native-plugin"
    skill = project / "skills" / "published"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Published\n", encoding="utf-8")

    init_result = _run_apm(project, "init", "--yes")
    assert init_result.returncode == 0, init_result.stderr
    assert not (project / ".apm").exists()

    pack_result = _run_apm(project, "pack")
    assert pack_result.returncode == 0, pack_result.stderr
    bundles = [path for path in (project / "build").iterdir() if path.is_dir()]
    assert len(bundles) == 1
    assert (bundles[0] / "skills" / "published" / "SKILL.md").is_file()
```

- [ ] **Step 3: Extend the existing `.apm/` authority e2e test**

Keep `test_pack_auto_includes_only_apm_authored_skills` and assert the warning
contains the skipped `skills/` directory, `.apm/` cause, and move/remove action.

- [ ] **Step 4: Run e2e tests RED then GREEN**

```bash
uv run --extra dev pytest tests/integration/test_pack_root_skills_e2e.py -xvs
```

Expected before Task 3: native-plugin journey fails because root skill is
absent. Expected after Task 3: all tests pass.

- [ ] **Step 5: Run mutation-break on the e2e trap**

Temporarily restore PR #2122's `if not package.has_declared_includes` gate.
Run:

```bash
uv run --extra dev pytest \
  tests/integration/test_pack_root_skills_e2e.py::test_init_then_pack_preserves_native_claude_skill \
  -q
```

Expected: FAIL. Restore the `.apm/` presence guard and confirm PASS.

- [ ] **Step 6: Commit the e2e slice**

```bash
git add tests/integration/test_pack_root_skills_e2e.py
git commit -m "test(pack): preserve native plugin init journey" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 5: Add `apm init` migration guidance

**Files:**
- Modify: `src/apm_cli/bundle/plugin_layout.py`
- Modify: `src/apm_cli/commands/_helpers.py:656-702`
- Modify: `tests/unit/test_init_command.py`

- [ ] **Step 1: Add a root convention detector**

Import the detector created in Task 3:

```python
from ..bundle.plugin_layout import find_plugin_root_sources
```

- [ ] **Step 2: Warn after writing `apm.yml`**

Use the existing console/logger helper in the init flow:

```python
native_dirs = find_plugin_root_sources(out_file.parent)
if native_dirs and not (out_file.parent / ".apm").is_dir():
    rendered = ", ".join(f"{name}/" for name in native_dirs)
    _rich_warning(
        f"Found plugin-native directories at the project root: {rendered}. "
        "They remain included by apm pack. Move publishable files under .apm/ "
        "when adopting the APM source layout."
    )
```

Do not create `.apm/` and do not move files.

- [ ] **Step 3: Add init warning coverage**

```python
def test_init_preserves_plugin_native_layout(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.chdir(tmp_dir)
        try:
            Path("skills").mkdir()

            result = self.runner.invoke(cli, ["init", "--yes"])

            assert result.exit_code == 0
            assert "Found plugin-native directories" in result.output
            assert "skills/" in result.output
            assert "remain included by apm pack" in result.output
            assert not Path(".apm").exists()
        finally:
            os.chdir(self.original_dir)
```

- [ ] **Step 4: Run focused init tests**

```bash
uv run --extra dev pytest tests/unit/test_init_command.py -q
```

Expected: all init tests pass.

- [ ] **Step 5: Commit the onboarding slice**

```bash
git add src/apm_cli/bundle/plugin_layout.py src/apm_cli/commands/_helpers.py tests/unit/test_init_command.py
git commit -m "feat(init): explain native plugin source layout" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 6: Align documentation and changelog

**Files:**
- Modify: `docs/src/content/docs/producer/pack-a-bundle.md`
- Modify: `docs/src/content/docs/producer/repo-shapes.md`
- Modify: `docs/src/content/docs/reference/cli/pack.md`
- Modify: `docs/src/content/docs/reference/manifest-schema.md`
- Modify: `docs/src/content/docs/getting-started/first-package.md`
- Modify: `packages/apm-guide/.apm/skills/apm-usage/package-authoring.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Replace includes-based layout claims**

Use this canonical sentence on each relevant documentation surface:

```text
When `.apm/` exists, local primitive content is sourced from `.apm/`.
Without `.apm/`, supported plugin-native root directories remain pack
sources. An explicit `includes` list remains exhaustive.
```

- [ ] **Step 2: Document mixed-layout warnings**

Explain that `.apm/` wins, root directories are skipped, pack succeeds, and the
warning tells the author to move or remove the directory.

- [ ] **Step 3: Preserve first-package native onboarding**

Keep the existing statement that a plugin can begin with no `.apm/` and
plugin-native convention directories. Add that `apm init` does not invalidate
that layout.

- [ ] **Step 4: Correct the changelog**

The entry must describe the user outcome:

```text
- Fixed plugin packing so `.apm/` becomes authoritative when present while
  native plugin convention directories remain packable before `.apm/`
  adoption.
```

- [ ] **Step 5: Commit documentation**

```bash
git add CHANGELOG.md docs/src/content/docs packages/apm-guide/.apm/skills/apm-usage/package-authoring.md
git commit -m "docs(pack): explain source layout authority" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 7: Full validation and shepherd convergence

**Files:**
- Verify all PR #2122 files.

- [ ] **Step 1: Run focused suites**

```bash
uv run --extra dev pytest tests/unit/test_plugin_exporter.py tests/unit/test_init_command.py -q
uv run --extra dev pytest tests/integration/test_pack_root_skills_e2e.py -q
```

Expected: all pass.

- [ ] **Step 2: Run every integration test**

```bash
bash scripts/test-integration.sh
```

Expected: exit 0 with no failures.

- [ ] **Step 3: Run the canonical lint contract**

```bash
uv run --extra dev ruff check src/ tests/ \
  && uv run --extra dev ruff format --check src/ tests/ \
  && uv run --extra dev python -m pylint --disable=all --enable=R0801 \
     --min-similarity-lines=10 --fail-on=R0801 src/apm_cli/ \
  && bash scripts/lint-auth-signals.sh
```

Expected: exit 0.

- [ ] **Step 4: Re-run `apm-review-panel`**

Fold all in-scope findings. Repeat implementation, focused tests, full
integration, lint, push, and CI recovery until the final posted panel verdict
is `ship_now`.

- [ ] **Step 5: Push safely**

```bash
git push --force-with-lease origin HEAD:fix/2054-pack-auto-includes
```

Expected: PR #2122 updates without overwriting unexpected remote work.

- [ ] **Step 6: Watch CI**

```bash
gh pr checks 2122 --repo microsoft/apm --watch
```

Expected: all required checks green on the final head SHA.

- [ ] **Step 7: Verify mergeability and posted verdict**

```bash
gh pr view 2122 --repo microsoft/apm \
  --json headRefOid,mergeable,mergeStateStatus,statusCheckRollup,comments
```

Expected: `mergeable=MERGEABLE`, CI green, and the final panel comment contains
`ship_now`.
