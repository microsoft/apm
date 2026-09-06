"""Mutation coverage for the Unix installation ownership boundary."""

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.transport_sparse_and_updates import (
    _check_unix_install_ownership,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-unix-install-ownership"

_RECOGNIZED_BUNDLE = """apm_is_recognized_bundle() {
    [ -f "$1/apm" ] && [ ! -L "$1/apm" ] && {
        { [ -f "$1/.apm-installed" ] && [ ! -L "$1/.apm-installed" ]; } ||
            { [ -f "$1/VERSION" ] && [ ! -L "$1/VERSION" ] &&
                [ -d "$1/_internal" ] && [ ! -L "$1/_internal" ]; }
    }
}
"""

_INSTALL_FIXTURE = f"""#!/bin/sh
# INSTALL_OWNERSHIP_BEGIN
{_RECOGNIZED_BUNDLE}
apm_probe_installation() {{
    _candidate_lib="$(dirname "$1")"
    if ! apm_is_recognized_bundle "$_candidate_lib"; then
        return 1
    fi
}}

apm_resolve_install_paths() {{
    :
}}

apm_require_owned_bundle() {{
    :
}}
# INSTALL_OWNERSHIP_END

apm_lib_dir_validate() {{
    _apm_lib_dir="$1"
    if [ -d "$_apm_lib_dir" ] && [ "$(ls -A "$_apm_lib_dir" 2>/dev/null)" ]; then
        if ! apm_is_recognized_bundle "$_apm_lib_dir"; then
            return 14
        fi
    fi
}}

apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm
apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm
apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm
apm_require_writable_directory "$APM_INSTALL_DIR"
apm_require_writable_directory "$(dirname "$APM_LIB_DIR")"
apm_require_owned_bundle
"""


@pytest.fixture
def unix_install_sources() -> dict[str, str]:
    """Return a minimal, isolated source inventory satisfying the ownership rule."""
    return {
        "install.sh": _INSTALL_FIXTURE,
        "src/apm_cli/commands/self_update.py": (
            'env["APM_SELF_UPDATE_SOURCE"] = os.path.abspath(\n'
        ),
    }


def _fixture_violations(tmp_path: Path, sources: dict[str, str]) -> tuple[Violation, ...]:
    """Run the rule directly against an in-memory provider inventory."""
    provider = FactsProvider(
        tmp_path,
        tuple(sources),
        registry=None,
        source_overrides=sources,
    )
    return _check_unix_install_ownership(provider)


def test_unix_install_bundle_identity_fixture_is_clean(
    tmp_path: Path, unix_install_sources: dict[str, str]
) -> None:
    """The exact canonical predicate and both consumers satisfy the static owner."""
    assert not _fixture_violations(tmp_path, unix_install_sources)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(_RECOGNIZED_BUNDLE, "", id="missing-definition"),
        pytest.param(
            _RECOGNIZED_BUNDLE,
            "apm_is_recognized_bundle() {\n    return 0\n}\n",
            id="predicate-replaced-by-return-zero",
        ),
        pytest.param(
            ' && [ ! -L "$1/apm" ]',
            "",
            id="launcher-symlink-check-removed",
        ),
        pytest.param(
            ' && [ ! -L "$1/.apm-installed" ]',
            "",
            id="marker-symlink-check-removed",
        ),
        pytest.param(
            ' && [ ! -L "$1/VERSION" ]',
            "",
            id="version-symlink-check-removed",
        ),
        pytest.param(
            ' && [ ! -L "$1/_internal" ]',
            "",
            id="internal-symlink-check-removed",
        ),
        pytest.param(
            "# INSTALL_OWNERSHIP_END\n",
            "if true; then\n"
            "    function apm_is_recognized_bundle { :; }\n"
            "fi\n"
            "# INSTALL_OWNERSHIP_END\n",
            id="second-indented-bash-owner",
        ),
        pytest.param(
            '    if ! apm_is_recognized_bundle "$_candidate_lib"; then\n',
            '    if [ ! -f "$_candidate_lib/.apm-installed" ]; then\n',
            id="discovery-rederives-identity",
        ),
        pytest.param(
            '        if ! apm_is_recognized_bundle "$_apm_lib_dir"; then\n',
            '        if [ ! -f "$_apm_lib_dir/.apm-installed" ]; then\n',
            id="deletion-rederives-identity",
        ),
        pytest.param(
            '        if ! apm_is_recognized_bundle "$_apm_lib_dir"; then\n',
            '        if [ -f "$_apm_lib_dir/VERSION" ]; then\n'
            "            return 0\n"
            "        fi\n"
            '        if ! apm_is_recognized_bundle "$_apm_lib_dir"; then\n',
            id="deletion-version-allowlist",
        ),
    ],
)
def test_unix_install_bundle_identity_mutations_fail_closed(
    tmp_path: Path,
    unix_install_sources: dict[str, str],
    old: str,
    new: str,
) -> None:
    """Identity removal, weakening, local re-derivation, or bypass must be rejected."""
    install = unix_install_sources["install.sh"]
    assert old in install
    unix_install_sources["install.sh"] = install.replace(old, new, 1)
    violations = _fixture_violations(tmp_path, unix_install_sources)
    assert any(violation.rule_id == RULE_ID for violation in violations)


def test_unix_install_bundle_identity_must_precede_discovery(
    tmp_path: Path, unix_install_sources: dict[str, str]
) -> None:
    """The canonical helper stays inside the ownership section before its first consumer."""
    install = unix_install_sources["install.sh"]
    unix_install_sources["install.sh"] = install.replace(_RECOGNIZED_BUNDLE, "", 1) + (
        "\n" + _RECOGNIZED_BUNDLE
    )
    violations = _fixture_violations(tmp_path, unix_install_sources)
    assert any(violation.rule_id == RULE_ID for violation in violations)


@pytest.mark.parametrize(
    ("path", "definition"),
    [
        (
            "src/apm_cli/commands/competing.py",
            "def apm_is_recognized_bundle(path):\n    return True\n",
        ),
        (
            "src/apm_cli/commands/competing.py",
            "class Competing:\n    def apm_is_recognized_bundle(self, path):\n        return True\n",
        ),
        (
            "src/apm_cli/commands/competing.py",
            "async def apm_is_recognized_bundle(path):\n    return True\n",
        ),
        (
            "scripts/competing.sh",
            "if true; then\n    apm_is_recognized_bundle() { :; }\nfi\n",
        ),
        (
            "scripts/competing.sh",
            "function apm_is_recognized_bundle () { :; }\n",
        ),
        (
            "scripts/competing.sh",
            "function apm_is_recognized_bundle { :; }\n",
        ),
    ],
)
def test_unix_install_bundle_identity_rejects_competing_owner_forms(
    tmp_path: Path,
    unix_install_sources: dict[str, str],
    path: str,
    definition: str,
) -> None:
    """Indented, async, and Bash definitions cannot create a second identity owner."""
    unix_install_sources[path] = definition
    violations = _fixture_violations(tmp_path, unix_install_sources)
    assert any(violation.rule_id == RULE_ID for violation in violations)


def test_unix_install_owner_registered_and_clean() -> None:
    """The owner registry and the CI entrypoint must include the same rule."""
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)
    report = run_selected_rules(ROOT, (RULE_ID,))
    assert rule.guard_ids == (RULE_ID,)
    assert not report.violations
    assert not report.failures


def test_unix_install_registered_guard_rejects_bundle_identity_return_zero() -> None:
    """The registered clean assertion fails if the canonical predicate becomes universal."""
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert _RECOGNIZED_BUNDLE in source
    mutated = source.replace(
        _RECOGNIZED_BUNDLE,
        "apm_is_recognized_bundle() {\n    return 0\n}\n",
        1,
    )
    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={"install.sh": mutated},
    )
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "mutation"),
    [
        ("install.sh", '\nsudo mkdir -p "$APM_LIB_DIR"\n'),
        (
            "src/apm_cli/commands/self_update.py",
            "\ndef apm_resolve_install_paths():\n    return '/usr/local/bin'\n",
        ),
        (
            "src/apm_cli/commands/self_update.py",
            "\nclass CompetingOwner:\n    def apm_resolve_install_paths(self):\n        pass\n",
        ),
        (
            "scripts/lint-auth-signals.sh",
            "\nif true; then\n    apm_probe_installation() { :; }\nfi\n",
        ),
        (
            "src/apm_cli/commands/self_update.py",
            "\nasync def apm_require_owned_bundle():\n    pass\n",
        ),
        (
            "scripts/lint-auth-signals.sh",
            "\nfunction apm_require_owned_bundle () { :; }\n",
        ),
        pytest.param(
            "scripts/lint-auth-signals.sh",
            "\nfunction apm_require_owned_bundle { :; }\n",
            id="bash-function-without-parentheses",
        ),
    ],
)
def test_unix_install_boundary_rejects_elevation_or_second_owner(path: str, mutation: str) -> None:
    """Reintroducing escalation or a competing owner must fail the static guard."""
    source = (ROOT / path).read_text(encoding="utf-8")
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides={path: source + mutation})
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


def test_unix_install_boundary_requires_self_update_identity() -> None:
    """Self-update cannot silently stop identifying the installation being updated."""
    path = "src/apm_cli/commands/self_update.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        'env["APM_SELF_UPDATE_SOURCE"] = os.path.abspath(', "ignored = os.path.abspath("
    )
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides={path: mutated})
    assert any(violation.rule_id == RULE_ID for violation in report.violations)
