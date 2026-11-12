#!/usr/bin/env python3
"""Render fixture JSON against the recommendation template's rendering rules.

This is a SPECIFICATION TEST, not a production renderer. The orchestrator LLM
applies the same rules described in the template comment block when rendering
in production; this script lets a maintainer eyeball the output offline and
confirms the rules collapse to a compact, scannable comment.

Usage:
    python3 render_eval.py
    python3 render_eval.py fixtures/01-ship-now-pr1084-shape.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PERSONA_LABELS = {
    "python-architect": "Python Architect",
    "cli-logging-expert": "CLI Logging Expert",
    "devx-ux-expert": "DevX UX Expert",
    "supply-chain-security-expert": "Supply Chain Security",
    "oss-growth-hacker": "OSS Growth Hacker",
    "auth-expert": "Auth Expert",
    "doc-writer": "Doc Writer",
    "test-coverage-expert": "Test Coverage",
}

PRINCIPLE_LABELS = {
    "portable_by_manifest": "Portable by manifest",
    "secure_by_default": "Secure by default",
    "governed_by_policy": "Governed by policy",
    "multi_harness_multi_host": "Multi-harness / multi-host",
    "oss_community_driven": "OSS community-driven",
    "pragmatic_as_npm": "Pragmatic as npm",
}


def humanize_persona(key: str) -> str:
    return PERSONA_LABELS.get(key, key)


def count_findings(findings: list[dict], severity: str) -> int:
    return sum(1 for f in findings if f.get("severity") == severity)


def render(fixture: dict) -> str:
    panelists = fixture["panelists"]
    ceo = fixture["ceo"]
    active = [p for p in panelists if p.get("active")]

    out: list[str] = []

    # Header: stance + headline. Top-loaded for the busy maintainer.
    stance = ceo["ship_recommendation"]["stance"]
    out.append(f"## APM Review Panel: `{stance}`")
    out.append("")
    out.append(f"> {ceo['headline']}")
    out.append("")

    notify = fixture.get("notify_audience") or []
    if notify:
        out.append(f"cc {' '.join(notify)} -- a fresh advisory pass is ready for your review.")
        out.append("")

    out.append(ceo["arbitration"])
    out.append("")

    if ceo.get("dissent_notes"):
        out.append(f"**Dissent.** {ceo['dissent_notes']}")
        out.append("")

    aligned = {k: v for k, v in (ceo.get("principle_alignment") or {}).items() if v}
    if aligned:
        names = ", ".join(PRINCIPLE_LABELS.get(k, k) for k in aligned)
        out.append(f"**Aligned with:** {names}")
        out.append("")

    if ceo.get("growth_amplification"):
        out.append(f"**Growth signal.** {ceo['growth_amplification']}")
        out.append("")

    # Per-persona summary table.
    out.append("### Panel summary")
    out.append("")
    out.append("| Persona | B | R | N | Takeaway |")
    out.append("|---|---|---|---|---|")
    for p in active:
        b = count_findings(p.get("findings", []), "blocking")
        r = count_findings(p.get("findings", []), "recommended")
        n = count_findings(p.get("findings", []), "nit")
        out.append(
            f"| {humanize_persona(p['persona'])} | {b} | {r} | {n} | {p['summary']} |"
        )
    out.append("")
    out.append("> B = blocking-severity findings, R = recommended, N = nits.")
    out.append("> Counts are signal strength, not gates. The maintainer ships.")
    out.append("")

    # Top follow-ups, capped at 5.
    followups = (ceo.get("recommended_followups") or [])[:5]
    if followups:
        n = len(followups)
        out.append(f"### Top {n} follow-ups")
        out.append("")
        for i, f in enumerate(followups, 1):
            blocking_tag = " *(blocking-severity)*" if f.get("blocking") else ""
            persona = humanize_persona(f["from_persona"])
            out.append(
                f"{i}. **[{persona}]{blocking_tag}** {f['summary']} -- {f['why']}"
            )
        out.append("")

    # Architecture diagrams: render only when supplied. Order: class_diagram,
    # component, sequence (matches python-architect.agent.md sections 1/2/3).
    arch = next(
        (p for p in active if p["persona"] == "python-architect"),
        None,
    )
    diagrams = (arch or {}).get("extras", {}).get("diagrams", {}) if arch else {}
    diagram_order = ["class_diagram", "component", "sequence"]
    if any(diagrams.get(k) for k in diagram_order):
        out.append("### Architecture")
        out.append("")
        for key in diagram_order:
            block = diagrams.get(key)
            if not block:
                continue
            out.append("```mermaid")
            out.append(block)
            out.append("```")
            out.append("")

    out.append("### Recommendation")
    out.append("")
    out.append(ceo["ship_recommendation"]["prose"])
    out.append("")

    out.append("---")
    out.append("")
    out.append("<details>")
    out.append("<summary>Full per-persona findings</summary>")
    out.append("")

    canonical_order = [
        "python-architect",
        "cli-logging-expert",
        "devx-ux-expert",
        "supply-chain-security-expert",
        "oss-growth-hacker",
        "auth-expert",
        "doc-writer",
        "test-coverage-expert",
    ]
    by_key = {p["persona"]: p for p in panelists}
    for key in canonical_order:
        p = by_key.get(key)
        if not p:
            continue
        if not p.get("active"):
            out.append(f"#### {humanize_persona(key)} -- inactive")
            out.append("")
            out.append(p.get("inactive_reason", "Not in scope for this PR."))
            out.append("")
            continue
        out.append(f"#### {humanize_persona(key)}")
        out.append("")
        findings = p.get("findings", [])
        if not findings:
            out.append("No findings.")
            out.append("")
            continue
        for f in findings:
            loc = ""
            if f.get("file"):
                loc = f" at `{f['file']}"
                if f.get("line"):
                    loc += f":{f['line']}"
                loc += "`"
            out.append(f"- **[{f['severity']}]** {f['summary']}{loc}")
            out.append(f"  {f['rationale']}")
            if f.get("suggestion"):
                out.append(f"  *Suggested:* {f['suggestion']}")
            ev = f.get("evidence")
            if ev:
                outcome = ev.get("outcome", "unknown")
                tf = ev.get("test_file", "")
                tn = ev.get("test_name", "")
                ref = tf + (f"::{tn}" if tn and tf else "") if tf else (tn or "(no test ref)")
                proves = ev.get("proves", "")
                principles = ev.get("principles", []) or []
                tags = (" [" + ",".join(principles) + "]") if principles else ""
                if outcome == "passed":
                    line = f"  *Proof (test {outcome}):* `{ref}`"
                elif outcome == "failed":
                    line = f"  *Proof (test FAILED):* `{ref}`"
                elif outcome == "missing":
                    line = f"  *Proof (test MISSING at):* `{ref}`"
                elif outcome == "manual":
                    line = f"  *Proof (manual only):* `{ref}`"
                else:
                    line = f"  *Proof ({outcome}):* `{ref}`"
                if proves:
                    line += f" -- proves: {proves}"
                line += tags
                out.append(line)
                ax = ev.get("assertion_excerpt")
                if ax:
                    ax_one = " ".join(ax.split())
                    if len(ax_one) > 200:
                        ax_one = ax_one[:197] + "..."
                    out.append(f"  `{ax_one}`")
        out.append("")
    out.append("</details>")
    out.append("")
    out.append(
        "<sub>This panel is advisory. It does not block merge. Re-apply the "
        "`panel-review` label after addressing feedback to re-run.</sub>"
    )
    return "\n".join(out)


def lint_ascii(text: str) -> list[str]:
    """ASCII-only enforcement (encoding.instructions.md)."""
    issues: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        for ch in line:
            cp = ord(ch)
            if ch == "\n" or ch == "\t":
                continue
            if cp < 0x20 or cp > 0x7E:
                issues.append(f"line {i}: non-ASCII char U+{cp:04X} ({ch!r})")
                break
    return issues


def main() -> int:
    here = Path(__file__).parent
    if len(sys.argv) > 1:
        paths = [Path(sys.argv[1])]
    else:
        paths = sorted((here / "fixtures").glob("*.json"))

    for path in paths:
        fixture = json.loads(path.read_text())
        rendered = render(fixture)
        out_path = path.with_suffix(".rendered.md")
        out_path.write_text(rendered + "\n")
        line_count = len(rendered.splitlines())
        char_count = len(rendered)
        ascii_issues = lint_ascii(rendered)
        status = "OK" if not ascii_issues else f"FAIL ({len(ascii_issues)} ASCII)"
        print(
            f"[{status}] {path.name} -> {out_path.name} "
            f"({line_count} lines, {char_count} chars)"
        )
        for issue in ascii_issues:
            print(f"  {issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
