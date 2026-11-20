"""Keep the pack credential-file refusal in ``bundle/credential_files.py``.

OpenAPM req-sc-007: ``apm pack`` refuses to pack any file whose name matches
the credential-file pattern set. The owner defines the set and the refusal;
every bundle entry point and the ``apm publish`` auto-pack call the refusal on its final file list before any
write or dry-run return. A new bundle entry point must join the known set,
so no format can bypass the check.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    GROUP,
    _count_checks,
    _count_re,
    _forbid_scan,
    _load,
    _paths_under,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "marketplace-integrations-pack-credential-files"
_OWNER = "src/apm_cli/bundle/credential_files.py"
_REFUSAL_SITES: tuple[tuple[str, str], ...] = (
    ("src/apm_cli/bundle/agent_plugin_exporter.py", "refuse_credential_files(file_map)"),
    (
        "src/apm_cli/bundle/packer.py",
        "refuse_credential_files(_bundle_file_paths(project_root, unique_files, path_mappings))",
    ),
    ("src/apm_cli/bundle/plugin_exporter.py", "refuse_credential_files(file_map)"),
    (
        "src/apm_cli/commands/publish.py",
        "refuse_credential_files(portable_relpath(f, project_root) for f in sources)",
    ),
)
# claude_plugin_exporter delegates to plugin_exporter.export_plugin_bundle.
_ENTRY_POINT_FILES = frozenset(
    {
        "src/apm_cli/bundle/agent_plugin_exporter.py",
        "src/apm_cli/bundle/claude_plugin_exporter.py",
        "src/apm_cli/bundle/packer.py",
        "src/apm_cli/bundle/plugin_exporter.py",
    }
)
_ENTRY_POINT = re.compile(r"^def (?:pack_bundle|export_\w+_bundle)\(")
_PATTERN_LITERAL = re.compile(r"[\"']id_ed25519[\"']")


def _check(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inv,
            RULE_ID,
            _OWNER,
            (
                ("re", r"^CREDENTIAL_FILE_PATTERNS\b", 1, "eq"),
                ("re", r"^def is_credential_file\(", 1, "eq"),
                ("re", r"^def refuse_credential_files\(", 1, "eq"),
            ),
            "the credential-file pattern set and refusal must stay in the owner",
        )
    )
    for path, call in _REFUSAL_SITES:
        findings.extend(
            _count_checks(
                provider,
                inv,
                RULE_ID,
                path,
                (("sub", call, 1, "eq"),),
                "each bundle format must refuse credential files before writing",
            )
        )
    for path in _paths_under(provider, "src/apm_cli/bundle/", (".py",)):
        if path in _ENTRY_POINT_FILES:
            continue
        facts, failures = _load(provider, inv, RULE_ID, path)
        if failures:
            findings.extend(failures)
            continue
        if _count_re(facts, _ENTRY_POINT):
            findings.append(
                violation(
                    RULE_ID,
                    path,
                    "new bundle entry point: call refuse_credential_files on its file "
                    "list and register it in the pack credential-files guard",
                )
            )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _PATTERN_LITERAL,
            "only bundle/credential_files.py may declare credential-file patterns",
            exempt=True,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=RULE_ID,
        group=GROUP,
        guard_ids=(RULE_ID,),
        description="apm pack refuses credential files through bundle/credential_files.py.",
        check=_check,
    ),
)

__all__ = ["RULES"]
