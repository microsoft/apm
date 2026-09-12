"""Exercise real smoke-report log parsing with mocked GitHub calls."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("log_text", "expected"),
    [
        ("FAILED tests/test_one.py::test_case\n", ["tests/test_one.py::test_case"]),
        (
            "\x1b[31mtests/test_one.py::TestSuite::test_case[param] ERROR\x1b[0m\n",
            ["tests/test_one.py::TestSuite::test_case[param"],
        ),
        ("ERROR tests/test_one.py::test_case,\n", ["tests/test_one.py::test_case"]),
        (
            "FAILED tests/test_one.py::test_case\n"
            "tests/test_one.py::test_case FAILED\n"
            "ERROR tests/test_two.py::test_case\n",
            ["tests/test_one.py::test_case", "tests/test_two.py::test_case"],
        ),
        ("tests/test_one.py::test_case PASSED\n", []),
        (
            "\n".join(f"FAILED tests/test_many.py::test_{number}" for number in range(35)),
            [f"tests/test_many.py::test_{number}" for number in range(30)],
        ),
        pytest.param(
            "FAILED tests/test_real.py::test_failed\ntests/test_slow.py::" + "!::" * 10_000 + "\n",
            ["tests/test_real.py::test_failed"],
            id="failure-like-input-cannot-trigger-exponential-backtracking",
        ),
    ],
)
def test_failure_report_parses_logs_within_deadline(log_text: str, expected: list[str]) -> None:
    """A child-process deadline catches synchronous regex stalls, not just async delays."""
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const { main } = require('./scripts/daily-release-smoke-signal.cjs');
const log = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const github = {
  paginate: async endpoint => endpoint === 'jobs'
    ? [{id: 1, name: 'Tests', conclusion: 'failure'}] : [],
  rest: {
    actions: {
      listJobsForWorkflowRun: 'jobs',
      downloadJobLogsForWorkflowRun: async () => ({data: log}),
    },
    issues: {
      getLabel: async () => ({}),
      listForRepo: 'issues',
      create: async payload => {
        console.log(JSON.stringify(payload.body));
        return {data: {number: 1}};
      },
    },
  },
};
main({
  github,
  context: {repo: {owner: 'example', repo: 'project'}, runId: 1, runNumber: 1,
    sha: 'a'.repeat(40), workflow: 'Daily smoke'},
  core: {info() {}, warning(message) {throw new Error(message);}},
}).catch(error => { console.error(error); process.exitCode = 1; });
""",
        ],
        input=json.dumps(log_text),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    lines = body.split("### Failing tests\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert lines == (
        [f"- `{name}`" for name in expected]
        if expected
        else ["- Not extractable from failed job logs."]
    )
