"""Hostile nesting never crashes the libyaml-backed lockfile loader.

libyaml's scanner and parser are iterative; composition runs in Python, so
deep nesting ends in the bounded ``yaml.YAMLError``. Each document is parsed
in a child process, where a C stack overflow would surface as a negative
return code instead of killing the test run.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-read-only-lockfile-path")]

_PROBE = """
import sys, yaml
from apm_cli.utils.yaml_io import load_machine_yaml_str
text = sys.stdin.read()
try:
    load_machine_yaml_str(text)
except yaml.YAMLError:
    print("refused")
else:
    print("accepted")
"""

_FALLBACK_PROBE = """
import yaml
yaml.__with_libyaml__ = False
from apm_cli.utils import yaml_io
print(yaml_io._BoundedCSafeLoader is yaml_io._BoundedSafeLoader)
"""


@pytest.mark.parametrize(
    "text",
    ["[" * 100_000, "- " * 50_000 + "x\n", "{a: " * 100_000],
    ids=["100k-flow-sequences", "50k-block-sequences", "100k-flow-mappings"],
)
def test_deep_nesting_is_refused_not_crashed(text: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        input=text,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"returncode {result.returncode}: {result.stderr[-1000:]}"
    assert result.stdout.split() == ["refused"]


def test_without_libyaml_the_pure_loader_is_used() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _FALLBACK_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert result.stdout.split() == ["True"]
