# Templates

Load at Step 4 (test) and Step 8 (ledger row). Copy, rename, and keep
every oracle call.

## A. Pytest lifecycle test on today's harness

Copy to `tests/integration/test_<promise>_lifecycle.py`. This skeleton
runs green as written against the in-tree CLI
(`APM_BINARY_PATH="$(uv run which apm)" uv run --extra dev pytest <file> -n 0 -q`);
replace the arrange / act / assert blocks with your cell. Policy stays
ON: no `--no-policy`.

```python
"""APMLifecycle: <one-line promise in user words> (<issue or promise id>).

PROMISE: <id, e.g. ownership.preserve_unowned> (source: <path or issue>)
CELL: OP=uninstall SRC=local PRIM=skill SCOPE=project TGT=claude
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_TARGET = "claude"
_SKILL_DIR = ".claude/skills/alpha-skill"
_USER_SKILL_FILE = ".claude/skills/my-own-skill/SKILL.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines ([+], [!], [x], ...) per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _assert_lock_matches_deployed(project: Path, snapshot: LifecycleStateSnapshot) -> None:
    """I1: every deployment record points at a path that exists."""
    for record in snapshot.deployment_records:
        assert (project / record.locator.value).exists(), record


def test_promise_name_here(tmp_path: Path, apm_binary_path: Path) -> None:
    # World: hermetic HOME/APM_HOME, local packages, no network.
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=60)
    # One factory owns both packages so relative dependencies resolve.
    packages = LocalPackageFactory(isolated.work_root)

    alpha = packages.create("alpha")
    packages.add_skill(alpha, "alpha-skill", "---\nname: alpha-skill\ndescription: d\n---\nBody\n")
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, alpha)

    def apm(*args: str) -> CommandResult:
        result = runner.run(args, cwd=project.root, env=env)
        _assert_output_oracles(result)
        return result

    def snap() -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))

    # GIVEN: converged installed state (no --no-policy unless it is the subject).
    installed = apm("install")
    assert installed.returncode == 0, _evidence(installed)
    before = snap()
    _assert_lock_matches_deployed(project.root, before)

    # I2: repeating the converged command changes no bytes.
    again = apm("install")
    assert again.returncode == 0, _evidence(again)
    assert snap() == before, "I2: converged install changed durable state"

    # Arrange the cell under test, e.g. a user-owned skill next to the deployed one.
    sibling = project.root / _USER_SKILL_FILE
    sibling.parent.mkdir(parents=True)
    sibling.write_text("user notes\n", encoding="ascii")

    # WHEN: the mutating command under test.
    result = apm("uninstall", "../alpha")  # local deps are selected by path
    assert result.returncode == 0, _evidence(result)  # I8: documented exit code
    after = snap()

    # THEN: decisive state facts, not stdout substrings.
    assert sibling.read_text(encoding="ascii") == "user notes\n"
    assert not (project.root / _SKILL_DIR).exists()
    _assert_lock_matches_deployed(project.root, after)

    # I3: the audit verdict agrees with the durable state.
    audit = apm("audit", "--ci")
    assert audit.returncode == 0, _evidence(audit)
```

Reusing the reference module instead: `_new_scenario`, `_publish`,
`_run_success` and `_assert_same_state` can be imported from
`tests.integration.test_required_lifecycle_state_machine`. Pass a fresh
directory (`_new_scenario(tmp_path / "world", apm_binary_path)`;
`IsolatedApmEnvironment.create` refuses an existing root), and do not
reuse its `_INSTALL_ARGS`, which carries `--no-policy`.

## B. Replay row (only when `tests/lifecycle/replay/` exists)

```yaml
id: T1/update/revoked/lsp
promises: [P-INV-I4, P-OWN-executable-trust-context]   # ids must resolve in the promise catalog
cell: {OP: update, SRC: git, PRIM: lsp, SCOPE: project, GATE: revoked-after-deploy}
world: {packages: [p-lsp, p-skill], targets: [copilot, claude], scope: project}
given:
  - apm install p-lsp p-skill
  - apm approve p-lsp
  - apm install
when:
  - apm deny p-lsp
  - apm update --yes
then:
  exit: 0
  deployed: expected        # reference model: LSP config gone, skill kept
  announce: retired         # I7
```

## C. Bug-ledger row

Append to `bugs` in `tests/fixtures/lifecycle_bug_ledger.json`, keeping
`bugs` sorted by `issue`:

```json
{
  "issue": 1234,
  "fixed_in": "0.37.0",
  "summary": "<user-visible law that was broken, one neutral sentence>",
  "failure_modes": ["ownership"],
  "properties": ["ownership.preserve_unowned"],
  "regression_tests": ["tests/integration/<file>.py::<test>"]
}
```

- `failure_modes` uses the ledger taxonomy: auth, cache, cleanup,
  idempotency, observation, outcome, ownership, portability, reference,
  routing, transaction.
- `properties` are ids from the ledger's `property_catalog`.
- Validate: `uv run --extra dev pytest tests/quality/test_lifecycle_bug_ledger.py -q`.
