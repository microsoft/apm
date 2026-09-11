"""Known-good and known-bad cases for the supplied standalone checker."""

import json
import runpy
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils.yaml_io import load_frontmatter

pytestmark = pytest.mark.component

CHECKER = (
    Path(__file__).resolve().parents[3]
    / "examples/contracts/first-contract/checks/check_handoff.py"
)


def _check(tmp_path: Path, content: str | None, *arguments: str) -> subprocess.CompletedProcess:
    notes = tmp_path / "notes.md"
    notes.write_text("- restore: Restore dependencies.\n", encoding="utf-8")
    output = tmp_path / "handoff.json"
    if content is not None:
        output.write_text(content, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(CHECKER), str(output), str(notes), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, 2),
        ("not JSON", 2),
        ("{}", 1),
        ("[]", 1),
        ('[{"source_id":"restore","summary":"","caution":"Read first"}]', 1),
        ('[{"source_id":"different","summary":"Restore","caution":"Read first"}]', 1),
        (
            '[{"source_id":"restore","summary":"Restore","caution":"Read first","extra":true}]',
            1,
        ),
        ('[{"source_id":"restore","summary":"Restore","caution":"Read first"}]', 0),
    ],
)
def test_check_protocol_distinguishes_failed_conditions_from_incomplete(
    tmp_path: Path, content: str | None, expected: int
) -> None:
    result = _check(tmp_path, content)
    assert result.returncode == expected, result.stderr
    assert result.stdout.strip()
    assert result.stderr == ""
    if expected == 0:
        assert result.stdout.strip() == "JSON format is valid; every source note has one entry."


def test_style_requirement_is_checked_without_corrupting_unicode(tmp_path: Path) -> None:
    payload = [
        {
            "source_id": "restore",
            "summary": "Caf\u00e9 notes",
            "caution": "Check first: review sources",
        }
    ]
    result = _check(
        tmp_path,
        json.dumps(payload, ensure_ascii=False),
        "--caution-prefix",
        "Check first: ",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "handoff.json").read_text(encoding="utf-8")) == payload


def test_missing_imported_style_is_a_failed_condition(tmp_path: Path) -> None:
    payload = [{"source_id": "restore", "summary": "Restore", "caution": "Read sources"}]
    result = _check(tmp_path, json.dumps(payload), "--caution-prefix", "Check first: ")
    assert result.returncode == 1
    assert "style criterion" in result.stdout


@pytest.mark.skipif(
    not hasattr(sys, "get_int_max_str_digits"),
    reason="Interpreter does not implement the JSON integer conversion limit",
)
def test_json_integer_parser_limit_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONINTMAXSTRDIGITS", "4300")
    result = _check(tmp_path, "9" * 5000)
    assert result.returncode == 2, result.stderr
    assert result.stdout.strip() == ("Could not read the source notes or parse the candidate JSON.")
    assert result.stderr == ""


def test_json_recursion_failure_is_incomplete(tmp_path: Path) -> None:
    assess = runpy.run_path(str(CHECKER))["assess"]
    source = tmp_path / "notes.md"
    source.write_text("- restore: Restore dependencies.\n", encoding="utf-8")
    output = tmp_path / "handoff.json"
    output.write_text("[]", encoding="utf-8")
    with patch("json.loads", side_effect=RecursionError("decoder nesting limit")):
        status, reason = assess(output, source)
    assert status == 2
    assert reason == "Could not read the source notes or parse the candidate JSON."


@pytest.mark.parametrize("fixture", ["first-contract", "reuse-contract"])
def test_example_frontmatter_preserves_the_checker_arguments(fixture: str) -> None:
    source = CHECKER.parents[2] / fixture / "handoff.contract.md"
    document = load_frontmatter(source)
    arguments = shlex.split(document.metadata["verify"]["handoff"])
    assert arguments[:4] == [
        "python3",
        "checks/check_handoff.py",
        "handoff.json",
        "notes.md",
    ]
    assert arguments[4:] == (
        ["--caution-prefix", "Check first: "] if fixture == "reuse-contract" else []
    )
