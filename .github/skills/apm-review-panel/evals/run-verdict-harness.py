#!/usr/bin/env python3
"""
Deterministic verdict harness for the apm-review-panel skill.

Validates two things end-to-end WITHOUT spinning up an LLM:
1. Synthetic panelist JSON returns conform to panelist-return-schema.json.
2. The orchestrator's deterministic verdict rule produces the expected
   verdict + label mapping per the eval scenarios.

This is genesis Step 8 evals gate, deterministic slice. It does NOT
prove that a real LLM panelist will return well-formed JSON (that
requires option B, the branch-pin end-to-end test). It DOES prove that
when a panelist returns well-formed JSON, the orchestrator math is
correct and the schema rejects malformed shapes.

Usage:
    uv run --with jsonschema python3 \
        .apm/skills/apm-review-panel/evals/run-verdict-harness.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PANELIST = json.loads(
    (ROOT / "assets" / "panelist-return-schema.json").read_text()
)
SCHEMA_CEO = json.loads((ROOT / "assets" / "ceo-return-schema.json").read_text())


def derive_verdict(panelists: list[dict]) -> tuple[str, str]:
    """
    Pure verdict derivation per SKILL.md execution checklist step 6:
        total_required = sum(len(p["required"]) for p in panelists if p.get("active", True))
        verdict = "APPROVE" if total_required == 0 else "REJECT"
    """
    total_required = sum(
        len(p["required"]) for p in panelists if p.get("active", True)
    )
    verdict = "APPROVE" if total_required == 0 else "REJECT"
    label = "panel-approved" if verdict == "APPROVE" else "panel-rejected"
    return verdict, label


def case_clean_pr() -> dict:
    """Synthetic returns for the docs-only PR scenario."""
    panelists = [
        {"persona": "python-architect", "required": [], "nits": []},
        {
            "persona": "cli-logging-expert",
            "required": [],
            "nits": [
                {
                    "summary": "Wording: 'aliasing' could be 'aliases'",
                    "rationale": "Reads more naturally to npm/pip users",
                    "file": "README.md",
                    "line": 142,
                }
            ],
        },
        {"persona": "devx-ux-expert", "required": [], "nits": []},
        {"persona": "supply-chain-security-expert", "required": [], "nits": []},
        {"persona": "oss-growth-hacker", "required": [], "nits": []},
        {
            "persona": "auth-expert",
            "active": False,
            "inactive_reason": "README.md has no auth surface; no fast-path file matched.",
            "required": [],
            "nits": [],
        },
    ]
    ceo = {
        "arbitration": (
            "Documentation-only change pointing fish users to an existing "
            "recipe. All five mandatory specialists agree no required actions; "
            "auth-expert correctly inactive."
        )
    }
    return {
        "name": "clean-pr",
        "panelists": panelists,
        "ceo": ceo,
        "expected_verdict": "APPROVE",
        "expected_label": "panel-approved",
    }


def case_rejected_pr() -> dict:
    """Synthetic returns for the inline-retry-loop PR scenario."""
    panelists = [
        {
            "persona": "python-architect",
            "required": [
                {
                    "summary": "Inline retry loop violates Strategy pattern in deps/",
                    "rationale": (
                        "Codebase already has chain-of-responsibility retry "
                        "via AuthResolver; reuse or extract a shared helper "
                        "instead of inlining a fixed loop."
                    ),
                    "file": "src/apm_cli/deps/github_downloader.py",
                    "line": 218,
                    "suggestion": "Extract retry into deps/_retry.py with same shape as AuthResolver chain.",
                }
            ],
            "nits": [],
        },
        {
            "persona": "cli-logging-expert",
            "required": [],
            "nits": [
                {
                    "summary": "Missing [!] warning when retry kicks in",
                    "rationale": "User-visible network blip; STATUS_SYMBOLS guidance applies.",
                    "file": "src/apm_cli/deps/github_downloader.py",
                }
            ],
        },
        {
            "persona": "devx-ux-expert",
            "required": [],
            "nits": [
                {
                    "summary": "retries=3 undocumented in CLI help",
                    "rationale": "Mental model gap; npm/pip users expect to see configurable retries surfaced.",
                }
            ],
        },
        {"persona": "supply-chain-security-expert", "required": [], "nits": []},
        {"persona": "oss-growth-hacker", "required": [], "nits": []},
        {
            "persona": "auth-expert",
            "active": True,
            "required": [],
            "nits": [
                {
                    "summary": "Consider 401-aware retry",
                    "rationale": "Stale-PAT handling already exists in AuthResolver; align.",
                }
            ],
        },
    ]
    ceo = {
        "arbitration": (
            "Retry behavior is sound but the inline loop misses the "
            "codebase's existing retry abstraction. Python Architect's "
            "required action holds; nits across logging/devx/auth align."
        ),
        "dissent_notes": "",
    }
    return {
        "name": "rejected-pr",
        "panelists": panelists,
        "ceo": ceo,
        "expected_verdict": "REJECT",
        "expected_label": "panel-rejected",
    }


def case_malformed_panelist() -> dict:
    """Negative case: panelist forgot the `nits` field. S4 gate must fail."""
    return {
        "name": "malformed-missing-nits",
        "panelist": {"persona": "python-architect", "required": []},
        "expect_schema_error": True,
    }


def case_unknown_persona() -> dict:
    """Negative case: persona enum violation."""
    return {
        "name": "malformed-unknown-persona",
        "panelist": {
            "persona": "unknown-persona",
            "required": [],
            "nits": [],
        },
        "expect_schema_error": True,
    }


def case_disposition_leak() -> dict:
    """Negative case: panelist tried to add a verdict field. additionalProperties:false catches."""
    return {
        "name": "malformed-disposition-leak",
        "panelist": {
            "persona": "python-architect",
            "required": [],
            "nits": [],
            "disposition": "APPROVE",
        },
        "expect_schema_error": True,
    }


def run_positive(case: dict) -> tuple[bool, str]:
    """Validate every panelist + CEO return, then check verdict."""
    notes = []
    for p in case["panelists"]:
        try:
            jsonschema.validate(p, SCHEMA_PANELIST)
        except jsonschema.ValidationError as e:
            return False, f"panelist {p.get('persona')} failed schema: {e.message}"
        notes.append(
            f"  - {p['persona']}: active={p.get('active', True)}, "
            f"required={len(p['required'])}, nits={len(p['nits'])}"
        )

    try:
        jsonschema.validate(case["ceo"], SCHEMA_CEO)
    except jsonschema.ValidationError as e:
        return False, f"CEO failed schema: {e.message}"

    verdict, label = derive_verdict(case["panelists"])
    if verdict != case["expected_verdict"]:
        return (
            False,
            f"expected verdict={case['expected_verdict']}, got {verdict}",
        )
    if label != case["expected_label"]:
        return False, f"expected label={case['expected_label']}, got {label}"

    return True, "\n".join(
        notes
        + [
            f"  -> verdict={verdict}, label={label} (expected matched)",
        ]
    )


def run_negative(case: dict) -> tuple[bool, str]:
    """Validate that schema rejects the malformed shape."""
    try:
        jsonschema.validate(case["panelist"], SCHEMA_PANELIST)
    except jsonschema.ValidationError as e:
        return True, f"correctly rejected: {e.message.splitlines()[0]}"
    return False, "schema accepted a malformed return; S4 gate would have let it through"


def main() -> int:
    cases_pos = [case_clean_pr(), case_rejected_pr()]
    cases_neg = [case_malformed_panelist(), case_unknown_persona(), case_disposition_leak()]

    ok = True
    print("APM Review Panel - Deterministic Verdict Harness")
    print("=" * 60)
    print()
    print("Positive cases (well-formed JSON, expected verdict):")
    for c in cases_pos:
        passed, msg = run_positive(c)
        marker = "[+] PASS" if passed else "[x] FAIL"
        print(f"{marker}  {c['name']}")
        print(msg)
        print()
        ok = ok and passed

    print("Negative cases (malformed JSON, S4 schema gate must reject):")
    for c in cases_neg:
        passed, msg = run_negative(c)
        marker = "[+] PASS" if passed else "[x] FAIL"
        print(f"{marker}  {c['name']}")
        print(f"  {msg}")
        print()
        ok = ok and passed

    print("=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
