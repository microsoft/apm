"""Keep every Codex app-server start on the one offline launcher (#4432).

``CodexRpc.__init__`` in ``codex_plugins/client.py`` is the only place that may
start ``codex app-server``. It starts it from the private launch directory
whose project configuration turns plugins off, trusts only that directory, and
inherits the caller's environment unchanged (no ``env=``), so proxy settings
still reach the process and no network access is hidden from the operator.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.marketplace_integration_shared import GROUP
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

GUARD_CODEX_OFFLINE_LAUNCH = "marketplace-integrations-codex-offline-launch"
_OWNER = "src/apm_cli/codex_plugins/client.py"
_SPAWN_LITERAL = '"app-server"'
_REQUIRED = (
    ('"app-server", "--stdio", *_offline_launch_args(self._launch_dir)', "trusted launch args"),
    ("cwd=self._launch_dir,", "the private launch directory as its working directory"),
    ("self._launch_dir = _offline_launch_dir()", "a fresh private launch directory"),
    ('handle.write("[features]\\nplugins = false\\n")', "plugins off in the launch config"),
    ("shutil.rmtree(self._launch_dir, ignore_errors=True)", "removal of the launch directory"),
)


def _owner_findings(text: str) -> list[Violation]:
    rule = GUARD_CODEX_OFFLINE_LAUNCH
    findings = [
        violation(rule, _OWNER, f"the Codex app-server launch must use {what}", line=1)
        for needle, what in _REQUIRED
        if needle not in text
    ]
    spawns = [n for n, line in enumerate(text.splitlines(), 1) if _SPAWN_LITERAL in line]
    if len(spawns) != 1:
        findings.append(
            violation(rule, _OWNER, "exactly one Codex app-server launch is allowed", line=1)
        )
    if "env=" in text:
        findings.append(
            violation(
                rule,
                _OWNER,
                "the Codex app-server must inherit the caller's environment (no env=)",
                line=1,
            )
        )
    return findings


def check_codex_offline_launch(provider: FactsProvider) -> tuple[Violation, ...]:
    """Codex app-server starts only through CodexRpc's offline launcher."""
    rule = GUARD_CODEX_OFFLINE_LAUNCH
    findings: list[Violation] = []
    for path in provider.inventory:
        if not (path.startswith("src/apm_cli/") and path.endswith(".py")):
            continue
        text, read_error = provider.source_cache.read(path)
        if text is None:
            findings.append(violation(rule, path, f"required source unavailable: {read_error}"))
            continue
        if path == _OWNER:
            findings.extend(_owner_findings(text))
        elif _SPAWN_LITERAL in text:
            line = next(n for n, row in enumerate(text.splitlines(), 1) if _SPAWN_LITERAL in row)
            findings.append(
                violation(
                    rule,
                    path,
                    f"only {_OWNER} may start the Codex app-server (CodexRpc offline launcher)",
                    line=line,
                )
            )
    if _OWNER not in provider.inventory:
        findings.append(violation(rule, _OWNER, "the Codex offline launcher must exist", line=1))
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=GUARD_CODEX_OFFLINE_LAUNCH,
        group=GROUP,
        guard_ids=(GUARD_CODEX_OFFLINE_LAUNCH,),
        description="Codex app-server starts only through CodexRpc's offline launcher.",
        check=check_codex_offline_launch,
    ),
)

__all__ = ["RULES"]
