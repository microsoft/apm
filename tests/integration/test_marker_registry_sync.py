"""Marker-registry sync invariants for the integration suite.

This test asserts the integrity contract surfaced in
``.apm/instructions/tests.instructions.md`` (section "Integration tests:
placement and markers"): the marker names referenced by the rule, declared
in ``pyproject.toml``, documented in
``docs/src/content/docs/contributing/integration-testing.md``, and wired
into ``tests/integration/conftest.py::_MARKER_CHECKS`` MUST stay in sync.

Why this matters: the rule tells future contributors and agents that

* ``pyproject.toml`` is the marker source of truth,
* the docs table is the canonical registry,
* the conftest predicate map is the gate logic.

If any of those drifts, the rule's pointer becomes misleading and a future
PR that adds an ungated network/runtime test will not be caught by the
collection-time gate it claims to honour.

The tests are hermetic (read-only on repo files); no marker required.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tomllib

# ---------------------------------------------------------------------------
# Resolve repo root from this file location -- the test relies on relative
# layout, not on a cwd assumption.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCS_REGISTRY = (
    REPO_ROOT / "docs" / "src" / "content" / "docs" / "contributing" / "integration-testing.md"
)
APM_RULE = REPO_ROOT / ".apm" / "instructions" / "tests.instructions.md"
CONFTEST = REPO_ROOT / "tests" / "integration" / "conftest.py"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pyproject_marker_names() -> set[str]:
    """Return the set of marker names declared in pyproject.toml."""
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    raw = data["tool"]["pytest"]["ini_options"]["markers"]
    out: set[str] = set()
    for line in raw:
        name = line.split(":", 1)[0].strip()
        if name:
            out.add(name)
    return out


def _docs_registry_marker_names() -> set[str]:
    """Parse the marker table in the docs registry and return marker names.

    Table rows have shape: ``| `marker_name` | precondition | how |``.
    """
    text = DOCS_REGISTRY.read_text(encoding="utf-8")
    out: set[str] = set()
    # Match the first cell content if it is a backtick-quoted identifier.
    row_re = re.compile(r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|", re.MULTILINE)
    for m in row_re.finditer(text):
        out.add(m.group(1))
    return out


def _conftest_marker_names() -> set[str]:
    """Return marker names that have a predicate registered in conftest."""
    # Static parse to avoid importing the conftest module under test.
    text = CONFTEST.read_text(encoding="utf-8")
    # Look for entries inside the _MARKER_CHECKS dict: '    "name": ('
    key_re = re.compile(r'^\s*"(requires_[a-z0-9_]+)"\s*:\s*\(', re.MULTILINE)
    return set(key_re.findall(text))


def _gating_markers_in_pyproject() -> set[str]:
    """Subset of pyproject markers that gate execution (requires_* + live).

    ``integration``, ``slow``, ``benchmark`` are taxonomy markers, not
    gates, and are intentionally excluded from the docs registry.
    """
    names = _pyproject_marker_names()
    return {n for n in names if n.startswith("requires_") or n == "live"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_pyproject_gating_marker_has_conftest_predicate() -> None:
    """A declared ``requires_*`` marker without a predicate is dead config.

    If pyproject declares ``requires_X`` but ``_MARKER_CHECKS`` has no
    entry for it, the marker never auto-skips and the gate is a lie.
    """
    declared = {n for n in _pyproject_marker_names() if n.startswith("requires_")}
    wired = _conftest_marker_names()
    missing = sorted(declared - wired)
    assert not missing, (
        f"requires_* markers in pyproject.toml missing a predicate in "
        f"tests/integration/conftest.py::_MARKER_CHECKS: {missing}"
    )


def test_every_conftest_predicate_is_declared_in_pyproject() -> None:
    """A wired predicate without a marker declaration warns with PytestUnknownMarkWarning."""
    declared = _pyproject_marker_names()
    wired = _conftest_marker_names()
    orphans = sorted(wired - declared)
    assert not orphans, (
        f"_MARKER_CHECKS entries with no matching pyproject.toml marker "
        f"declaration (would emit PytestUnknownMarkWarning): {orphans}"
    )


def test_every_gating_marker_is_documented_in_registry() -> None:
    """Every ``requires_*`` / ``live`` marker MUST appear in the docs table.

    The instructions rule tells agents and humans to consult the docs
    table as the canonical registry. A marker missing from the table is
    invisible to anyone following the rule.
    """
    gating = _gating_markers_in_pyproject()
    documented = _docs_registry_marker_names()
    missing = sorted(gating - documented)
    assert not missing, (
        f"Gating markers declared in pyproject.toml but missing from "
        f"docs/src/content/docs/contributing/integration-testing.md "
        f"registry table: {missing}"
    )


def test_docs_registry_only_names_declared_markers() -> None:
    """The docs table must not advertise a marker that does not exist."""
    declared = _pyproject_marker_names()
    documented = _docs_registry_marker_names()
    phantom = sorted(documented - declared)
    assert not phantom, (
        f"Markers documented in the docs registry but not declared in pyproject.toml: {phantom}"
    )


def test_apm_rule_only_names_declared_markers() -> None:
    """Marker names cited in the APM instructions rule must really exist.

    Scans for ``requires_*`` tokens in the rule body and asserts each one
    matches a declared marker (or the documented ``requires_runtime_<name>``
    placeholder).
    """
    declared = _pyproject_marker_names()
    # Allow the literal placeholder used in the docs/quick-map.
    declared_with_placeholder = declared | {"requires_runtime_<name>"}
    body = APM_RULE.read_text(encoding="utf-8")
    # Match identifiers OR the placeholder shape inside backticks / plain.
    token_re = re.compile(r"\brequires_(?:runtime_<name>|[a-z][a-z0-9_]*)")
    cited = set(token_re.findall(body))
    # Re-prefix because the regex captured only the suffix; rebuild full names.
    # (The regex above intentionally returns the full token thanks to \b
    # anchoring and the group inside; re-derive with findall on full pattern.)
    full_re = re.compile(r"\brequires_(?:runtime_<name>|[a-z][a-z0-9_]+)")
    cited = set(full_re.findall(body))
    unknown = sorted(cited - declared_with_placeholder)
    assert not unknown, (
        f"Marker names cited in .apm/instructions/tests.instructions.md "
        f"that are not declared in pyproject.toml: {unknown}"
    )


def test_integration_tests_use_pytestmark_not_runtime_self_skip() -> None:
    """Tests must declare gates via ``pytestmark``, not runtime ``os.getenv``.

    The rule says: never write ``if not os.getenv("APM_E2E_TESTS"):
    pytest.skip(...)`` inside a test body. Use module-level ``pytestmark =
    pytest.mark.requires_e2e_mode`` instead. This guard catches future
    regressions of that pattern in ``tests/integration/test_*.py``.

    ``conftest.py`` is intentionally exempt: the marker registry itself
    must read the env vars to implement the gate.
    """
    gate_env_vars = (
        "APM_E2E_TESTS",
        "APM_RUN_INTEGRATION_TESTS",
        "APM_RUN_INFERENCE_TESTS",
        "APM_TEST_ADO_BEARER",
    )
    # Two-line window pattern: an os.getenv on a gate var followed by
    # pytest.skip on the next non-blank line (or same line).
    offenders: list[str] = []
    this_file = Path(__file__).resolve()
    for path in sorted(INTEGRATION_DIR.glob("test_*.py")):
        if path.resolve() == this_file:
            # The lint itself names the env var strings literally; skip.
            continue
        text = path.read_text(encoding="utf-8")
        for var in gate_env_vars:
            # Heuristic: an os.getenv(...gate var...) call AND a pytest.skip
            # in the SAME function body. We check for the var name appearing
            # within 200 chars of pytest.skip.
            for m in re.finditer(rf'os\.getenv\(\s*[\'"]{var}[\'"]', text):
                window = text[m.start() : m.start() + 400]
                if "pytest.skip" in window or "pytest.exit" in window:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: gates on {var}")
                    break
    assert not offenders, (
        "Integration test files use runtime os.getenv self-skip on a gate "
        "env var. Replace with a module-level pytestmark = pytest.mark."
        "requires_* marker (see .apm/instructions/tests.instructions.md):\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Sanity: confirm tomllib is available (Python 3.11+). All CI matrix
# entries currently use 3.12+; this test is the canary if that changes.
# ---------------------------------------------------------------------------


def test_tomllib_available() -> None:
    """tomllib was added in 3.11; CI runs on 3.12. Guard the assumption."""
    assert sys.version_info >= (3, 11), "This suite needs Python 3.11+ for tomllib"
