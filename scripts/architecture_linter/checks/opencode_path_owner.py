"""OpenCode user-configuration path ownership checks."""

from __future__ import annotations

import re
from collections.abc import Iterable

from scripts.architecture_linter.checks.registry_shared import _SRC, GROUP, _python_paths
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_OWNER = "src/apm_cli/integration/opencode_paths.py"
_ENV_READ = re.compile(
    r"(?:os\.)?(?:environ\.get|getenv|environ\[)[^\n]*(?:OPENCODE_CONFIG_DIR|XDG_CONFIG_HOME)"
)
_DEFAULT_PATH = 'Path.home() / ".config" / "opencode"'


def _check_opencode_path_owner(provider: FactsProvider) -> Iterable[Violation]:
    rule_id = "registry_delegation.opencode_path_owner"
    owner, failures = checked_facts(provider, _OWNER, rule_id, require_python=True)
    findings = list(failures)
    if failures:
        return findings

    owner_text = source_text(owner)
    for label, pattern in (
        ("OPENCODE_CONFIG_DIR", r'os\.environ\.get\("OPENCODE_CONFIG_DIR"'),
        ("XDG_CONFIG_HOME", r'os\.environ\.get\("XDG_CONFIG_HOME"'),
        (_DEFAULT_PATH, re.escape(_DEFAULT_PATH)),
    ):
        if len(re.findall(pattern, owner_text)) != 1:
            findings.append(
                violation(
                    rule_id,
                    _OWNER,
                    f"OpenCode path owner must contain exactly one {label!r}",
                )
            )

    for path in _python_paths(provider, under=_SRC, exclude=(_OWNER,)):
        facts, read_failures = checked_facts(provider, path, rule_id, require_python=True)
        if read_failures:
            findings.extend(read_failures)
            continue
        for line_number, line in enumerate(facts.lines, start=1):
            if _ENV_READ.search(line) or _DEFAULT_PATH in line:
                findings.append(
                    violation(
                        rule_id,
                        path,
                        "OpenCode configuration paths must route through integration/opencode_paths.py",
                        line=line_number,
                    )
                )
    return findings


RULES: tuple[Rule, ...] = (
    Rule(
        id="registry_delegation.opencode_path_owner",
        group=GROUP,
        guard_ids=("hooks-integrations-opencode-path-owner",),
        description="OpenCode configuration path resolution must have one canonical owner.",
        check=_check_opencode_path_owner,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
