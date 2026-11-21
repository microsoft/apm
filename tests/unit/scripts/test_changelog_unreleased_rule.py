"""The [Unreleased] edit rule: only a net-new bullet since the merge base fails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]

SCRIPT = Path(__file__).parents[3] / ".apm/skills/cut-release/scripts/changelog_fragments.py"
PREAMBLE = "# Changelog\n\nIntro.\n\n"
PREVIOUS = "## [0.1.0] - 2026-01-01\n\n### Fixed\n\n- Old fix. (#1)\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cut_release_changelog_rule", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fragments = _load()


def _changelog(*unreleased: str) -> str:
    return PREAMBLE + "## [Unreleased]\n\n### Fixed\n\n" + "".join(unreleased) + "\n" + PREVIOUS


@pytest.mark.parametrize(
    ("base", "head", "added"),
    [
        (_changelog("- A. (#1)\n"), _changelog("- A. (#1)\n", "- B. (#2)\n"), ["- B. (#2)"]),
        (_changelog("- A. (#1)\n"), _changelog("- A, reworded. (#1)\n"), []),
        (_changelog("- A. (#1)\n"), _changelog(), []),
        (_changelog("- A. (#1)\n"), _changelog("- A. (#1)\n"), []),
        (_changelog(), _changelog("- A. (#1)\n", "- A. (#1)\n"), ["- A. (#1)", "- A. (#1)"]),
        (_changelog(), _changelog().replace("- Old fix. (#1)", "- Old fix. (#1)\n- New."), []),
        (PREAMBLE + PREVIOUS, _changelog("- A. (#1)\n"), ["- A. (#1)"]),
    ],
    ids=["added", "reworded", "released", "unchanged", "duplicate", "older-block", "new-head"],
)
def test_unreleased_rule_fails_only_on_net_new_bullets(
    base: str, head: str, added: list[str]
) -> None:
    assert fragments.new_unreleased_bullets(base, head) == added
