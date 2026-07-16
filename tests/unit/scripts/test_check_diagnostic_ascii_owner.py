"""Unit tests for the printable agent-diagnostic authority checker."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_diagnostic_ascii_owner.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_diagnostic_ascii_owner", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def checker() -> ModuleType:
    return _load_checker()


@pytest.fixture()
def repo_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src/apm_cli/integration").mkdir(parents=True)
    (root / "src/apm_cli/utils").mkdir(parents=True)
    for relative in (
        "src/apm_cli/utils/diagnostics.py",
        "src/apm_cli/integration/agent_integrator.py",
        "src/apm_cli/integration/opencode_frontmatter.py",
    ):
        source = REPO_ROOT / relative
        target = root / relative
        shutil.copy2(source, target)
    return root


def test_real_repository_passes(checker) -> None:
    assert checker.check(REPO_ROOT) == []


def test_retired_private_helper_is_rejected(repo_copy: Path, checker) -> None:
    consumer = repo_copy / "src/apm_cli/integration/opencode_frontmatter.py"
    consumer.write_text(
        consumer.read_text(encoding="utf-8")
        + "\n\ndef _ascii_safe_name(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )

    violations = checker.check(repo_copy)

    assert any("_ascii_safe_name" in violation.message for violation in violations)


def test_renamed_local_ascii_implementation_is_rejected(repo_copy: Path, checker) -> None:
    consumer = repo_copy / "src/apm_cli/integration/agent_integrator.py"
    source = consumer.read_text(encoding="utf-8")
    source = source.replace(
        "from apm_cli.utils.diagnostics import printable_ascii_text",
        "def sanitize_display(value: str) -> str:\n"
        '    encoded = value.encode("ascii", "replace").decode("ascii")\n'
        "    return ''.join('?' if ord(char) == 0x7F else char for char in encoded)\n"
        "\nprintable_ascii_text = sanitize_display",
    )
    consumer.write_text(source, encoding="utf-8")

    violations = checker.check(repo_copy)

    assert any("reimplement it locally" in violation.message for violation in violations)


def test_decorative_owner_call_cannot_hide_regex_override(
    repo_copy: Path,
    checker,
) -> None:
    """A real rendered value must flow from the owner, not a decorative call."""
    consumer = repo_copy / "src/apm_cli/integration/opencode_frontmatter.py"
    source = consumer.read_text(encoding="utf-8")
    source = source.replace(
        "def validate_opencode_frontmatter(",
        "def _display_safe(value: str) -> str:\n"
        '    return re.sub(r"[^ -~]", "?", value)\n\n\n'
        "def validate_opencode_frontmatter(",
    )
    source = source.replace(
        "safe_name = printable_ascii_text(source.name)",
        "safe_name = printable_ascii_text(source.name)\n    safe_name = _display_safe(source.name)",
    )
    consumer.write_text(source, encoding="utf-8")

    violations = checker.check(repo_copy)

    assert any("local normalization path" in violation.message for violation in violations)
    assert any(
        "derive rendered diagnostic identity directly" in violation.message
        for violation in violations
    )


def test_dead_branch_owner_call_cannot_hide_raw_agent_alias(
    repo_copy: Path,
    checker,
) -> None:
    """An unreachable owner call must not legitimize a live raw alias."""
    consumer = repo_copy / "src/apm_cli/integration/agent_integrator.py"
    source = consumer.read_text(encoding="utf-8")
    source = source.replace(
        "        if diagnostics is None:\n"
        "            return\n"
        "        diagnostics.warn(\n"
        "            message=(\n"
        '                f"Codex agent {printable_ascii_text(source.name)}: {issue}. "\n',
        "        if diagnostics is None:\n"
        "            return\n"
        "        raw_name = source.name\n"
        "        diagnostics.warn(\n"
        "            message=(\n"
        '                f"Codex agent '
        '{raw_name if True else printable_ascii_text(source.name)}: {issue}. "\n',
        1,
    )
    consumer.write_text(source, encoding="utf-8")

    violations = checker.check(repo_copy)

    assert any("assign raw diagnostic identity" in item.message for item in violations)


def test_sanitized_identifier_cannot_share_message_with_raw_name(
    repo_copy: Path,
    checker,
) -> None:
    """A sanitized prefix must not excuse a second raw interpolation."""
    consumer = repo_copy / "src/apm_cli/integration/opencode_frontmatter.py"
    source = consumer.read_text(encoding="utf-8").replace(
        "f\"OpenCode agent '{identifier}' has tools as {kind}; \"",
        "f\"OpenCode agent '{identifier}' (raw file: {source.name}) has tools as {kind}; \"",
        1,
    )
    consumer.write_text(source, encoding="utf-8")

    violations = checker.check(repo_copy)

    assert any("must not render raw source.name" in item.message for item in violations)


def test_opencode_wrapper_package_field_must_use_owner(
    repo_copy: Path,
    checker,
) -> None:
    """The wrapper's Diagnostic.package field is also terminal output."""
    consumer = repo_copy / "src/apm_cli/integration/agent_integrator.py"
    source = consumer.read_text(encoding="utf-8").replace(
        "package=printable_ascii_text(package_name),",
        "package=package_name,",
        1,
    )
    consumer.write_text(source, encoding="utf-8")

    violations = checker.check(repo_copy)

    assert any(
        "AgentIntegrator._warn_opencode_frontmatter must derive" in item.message
        for item in violations
    )
    assert any(
        "must not render raw source.name or package_name" in item.message for item in violations
    )


def test_missing_owner_call_is_rejected(repo_copy: Path, checker) -> None:
    consumer = repo_copy / "src/apm_cli/integration/opencode_frontmatter.py"
    source = consumer.read_text(encoding="utf-8").replace(
        "safe_name = printable_ascii_text(source.name)",
        "safe_name = source.name",
    )
    consumer.write_text(source, encoding="utf-8")

    violations = checker.check(repo_copy)

    assert any(
        "validate_opencode_frontmatter must derive" in violation.message for violation in violations
    )


def test_missing_configured_consumer_fails_closed(
    repo_copy: Path,
    checker,
) -> None:
    (repo_copy / "src/apm_cli/integration/agent_integrator.py").unlink()

    with pytest.raises(FileNotFoundError, match=r"agent_integrator\.py"):
        checker.check(repo_copy)
