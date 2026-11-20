"""Deterministic scripts bundled with the release-acceptance skill."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / ".apm/skills/release-acceptance/scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"release_acceptance_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture = _load("capture")
findings = _load("findings")
journeys = _load("journeys")
brief_lint = _load("brief_lint")
world = _load("world")


def _finding(**overrides: object) -> dict:
    base = {
        "title": "update suggests an option it does not accept",
        "severity": "should-fix",
        "step": "E01.7",
        "observed": "Error: No such option: --no-policy",
        "expected": "the hint names a working command",
        "repro": "repros/e01_7.sh",
        "sha": "abc1234",
        "owner_file": "src/apm_cli/core/command_logger.py:964",
    }
    base.update(overrides)
    return base


class TestLineLint:
    def test_flags_non_ascii_duplicates_and_stale_names(self) -> None:
        out = "\u250f\u2501\u2501\nInstalled 1 skill here\nInstalled 1 skill here\nrun python -m apm_cli install\n"
        rules = {issue["rule"] for issue in capture.lint_lines(out, "", 0)}
        assert rules == {"non-ascii", "duplicate-line", "stale-command-name"}

    def test_flags_failure_without_stderr_and_errors_on_stdout(self) -> None:
        rules = {issue["rule"] for issue in capture.lint_lines("[x] Refusing to install\n", "", 1)}
        assert rules == {"error-without-stderr", "error-on-stdout"}

    def test_clean_success_output_has_no_issues(self) -> None:
        out = "[+] acme/tools #v1.0.0\n|-- 1 skill integrated -> .claude/skills/\n"
        assert capture.lint_lines(out, "", 0) == []

    def test_separator_lines_are_not_duplicates(self) -> None:
        assert capture.lint_lines("-" * 40 + "\n" + "-" * 40 + "\n", "", 0) == []

    def test_wrapped_tails_of_distinct_warnings_are_not_duplicates(self) -> None:
        tail = "allowed-tools, argument-hint, description, input, model."
        out = (
            "[!] Claude command review: frontmatter keys not supported and were dropped: mode, supported keys are\n"
            f"{tail}\n"
            "[!] Claude command refactor: frontmatter keys not supported and were dropped: mode, supported keys are\n"
            f"{tail}\n"
        )
        assert [issue["rule"] for issue in capture.lint_lines(out, "", 0)] == []

    def test_tails_wrapped_after_a_colon_are_not_duplicates(self) -> None:
        # Shape captured from `apm install` at 80 columns: Rich wraps right after "keys:".
        out = (
            "  [!] [pkg] Claude command accessibility-audit: frontmatter keys \n"
            "not supported for claude commands and were dropped: mode. Supported keys: \n"
            "allowed-tools, argument-hint, description, input, model.\n"
            "  [!] [pkg] Claude command design-review: frontmatter keys not \n"
            "supported for claude commands and were dropped: mode. Supported keys: \n"
            "allowed-tools, argument-hint, description, input, model.\n"
        )
        assert capture.lint_lines(out, "", 0) == []

    def test_list_items_after_a_long_colon_line_stay_separate(self) -> None:
        head = "[x] --frozen: deployed files do not match the hashes recorded in apm.lock:"
        out = f"{head}\n  - a.md: expected sha256:1\n  - b.md: expected sha256:2\n"
        assert capture.unwrap(out) == [
            head,
            "  - a.md: expected sha256:1",
            "  - b.md: expected sha256:2",
        ]

    def test_help_screens_skip_the_duplicate_check(self) -> None:
        out = "  --ssh   Prefer SSH transport for git\n  --ssh   Prefer SSH transport for git\n"
        assert capture.lint_lines(out, "", 0, help_output=True) == []


class TestHints:
    def test_extracts_quoted_commands_and_skips_placeholders(self) -> None:
        text = "[i] Run 'apm install --force' to restore.\nTry `apm approve <package>` next.\n"
        values = [hint["value"] for hint in capture.extract_hints(text, None)]
        assert values == ["apm install --force"]

    def test_extracts_verb_hints_and_ignores_help_tables(self) -> None:
        text = (
            "[i] Run apm compile to refresh AGENTS.md.\n"
            "| apm install   | Install dependencies |\n"
            "Share with: apm install ./build/project-1.0.0\n"
        )
        values = [hint["value"] for hint in capture.extract_hints(text, None)]
        assert values == ["apm compile", "apm install ./build/project-1.0.0"]

    def test_extracts_next_step_hints_from_panel_rows(self) -> None:
        text = (
            "| * Create a plugin:        apm plugin init                               |\n"
            "| * Pack as Agent Plugins:  apm pack --format agent-plugin                |\n"
        )
        values = [hint["value"] for hint in capture.extract_hints(text, None)]
        assert values == ["apm plugin init", "apm pack --format agent-plugin"]

    def test_joins_hard_wrapped_hints(self) -> None:
        first = "[!] Kept 1 local edit in .github/instructions/kit.instructions.md; run"
        text = f"{first.ljust(72)}\n'apm install --force' to overwrite it.\n"
        assert [hint["value"] for hint in capture.extract_hints(text, None)] == [
            "apm install --force"
        ]

    def test_resolves_the_bare_docs_root(self, tmp_path: Path) -> None:
        (tmp_path / "index.mdx").write_text("# APM\n", encoding="utf-8")
        hints = capture.extract_hints("Docs: https://microsoft.github.io/apm\n", tmp_path)
        assert [(hint["value"], hint["resolves"]) for hint in hints] == [
            ("https://microsoft.github.io/apm", True)
        ]

    def test_resolves_docs_urls_and_anchors(self, tmp_path: Path) -> None:
        page = tmp_path / "consumer" / "installing-from-marketplaces.md"
        page.parent.mkdir()
        page.write_text("# Title\n\n## Install patterns\n", encoding="utf-8")
        base = "https://microsoft.github.io/apm/consumer/installing-from-marketplaces/"
        hints = capture.extract_hints(f"see {base}#install-patterns and {base}#missing\n", tmp_path)
        assert [(h["value"].split("#")[1], h["resolves"]) for h in hints] == [
            ("install-patterns", True),
            ("missing", False),
        ]


class TestJourneyLoader:
    def test_refuses_when_the_catalogue_is_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(journeys.CatalogueError, match="catalogue unavailable"):
            journeys.load(tmp_path, None)

    def test_reads_the_program_index_and_normalizes_steps(self, tmp_path: Path) -> None:
        index = tmp_path / "tests/lifecycle/journeys/index.yaml"
        index.parent.mkdir(parents=True)
        index.write_text(
            "journeys:\n"
            "  - id: J01\n    persona: consumer\n    steps:\n"
            "      - {n: 1, cmd: apm --version, exit: 0}\n"
            "      - {cmd: apm init -y}\n",
            encoding="utf-8",
        )
        result = journeys.load(tmp_path, None)
        assert result["embedded"] is False
        assert [step["id"] for step in result["journeys"][0]["steps"]] == ["J01.1", "J01.2"]
        assert result["counts"] == {"journeys": 1, "steps": 2, "consumer": 1}

    def test_reads_index_yaml_field_names(self, tmp_path: Path) -> None:
        index = tmp_path / "tests/lifecycle/journeys/index.yaml"
        index.parent.mkdir(parents=True)
        index.write_text(
            "schema_version: 1\n"
            "journeys:\n"
            "  - id: J03\n    persona: consumer\n    steps:\n"
            "      - n: 1\n        cmd: apm install --frozen\n        kind: apm\n"
            "        argv: [[apm, install, --frozen]]\n        exit: 0\n"
            "        key_lines: ['[i] Deployed files verified']\n"
            "        forbidden_lines: [Traceback]\n",
            encoding="utf-8",
        )
        step = journeys.load(tmp_path, None)["journeys"][0]["steps"][0]
        assert (step["kind"], step["argv"], step["must_print"], step["must_not_print"]) == (
            "apm",
            [["apm", "install", "--frozen"]],
            ["[i] Deployed files verified"],
            ["Traceback"],
        )

    def test_accepts_an_operator_supplied_json_catalogue(self, tmp_path: Path) -> None:
        path = tmp_path / "devx-journeys.json"
        doc = {
            "meta": {},
            "journeys": [
                {"id": "E01", "persona": "enterprise", "steps": [{"n": 3, "cmd": "apm audit --ci"}]}
            ],
        }
        path.write_text(json.dumps(doc), encoding="utf-8")
        assert journeys.load(tmp_path, str(path))["journeys"][0]["steps"][0]["id"] == "E01.3"

    @pytest.mark.parametrize(
        "doc, message",
        [
            ({"items": []}, "'journeys' list"),
            (
                {"journeys": [{"id": "J01", "persona": "tourist", "steps": [{"cmd": "x"}]}]},
                "persona",
            ),
            ({"journeys": [{"id": "J01", "persona": "consumer", "steps": []}]}, "non-empty"),
            ({"journeys": [{"id": "J01", "persona": "consumer", "steps": [{"n": 1}]}]}, "'cmd'"),
            (
                {
                    "journeys": [
                        {"id": "J01", "persona": "consumer", "steps": [{"cmd": "a"}]},
                        {"id": "J01", "persona": "consumer", "steps": [{"cmd": "b"}]},
                    ]
                },
                "duplicate",
            ),
        ],
    )
    def test_rejects_malformed_catalogues(self, doc: dict, message: str) -> None:
        with pytest.raises(journeys.CatalogueError, match=message):
            journeys.normalize(doc, "catalogue")


class TestLedger:
    def test_add_assigns_ids_and_drops_duplicates(self) -> None:
        ledger = {"sha": None, "findings": []}
        result = findings.add(
            ledger,
            [
                _finding(),
                _finding(title="same bug, other words"),
                _finding(step="J07.2", observed="other"),
            ],
        )
        assert result["added"] == ["RA-001", "RA-002"]
        assert len(result["duplicates"]) == 1

    @pytest.mark.parametrize(
        "override, message",
        [
            ({"severity": "critical"}, "severity"),
            ({"repro": ""}, "missing"),
            ({"observed": "caf\u00e9"}, "ASCII"),
        ],
    )
    def test_add_rejects_invalid_findings(self, override: dict, message: str) -> None:
        with pytest.raises(findings.LedgerError, match=message):
            findings.add({"sha": None, "findings": []}, [_finding(**override)])

    def test_gate_waits_for_verification_then_blocks_on_a_confirmed_blocker(self) -> None:
        ledger = {"sha": None, "findings": []}
        findings.add(
            ledger,
            [
                _finding(severity="blocker"),
                _finding(step="J01.3", observed="nit text", severity="nit"),
            ],
        )
        row = findings.gate_row(ledger, "abc1234")
        assert (row["verdict"], row["open_blockers"], row["unverified_blockers"]) == (
            "PENDING-VERIFICATION",
            [],
            ["RA-001"],
        )
        findings.verify(ledger, [{"id": "RA-001", "verdict": "CONFIRMED"}])
        row = findings.gate_row(ledger, "abc1234")
        assert (row["verdict"], row["open_blockers"], row["unverified_blockers"]) == (
            "NO-GO",
            ["RA-001"],
            [],
        )
        ledger["findings"][0]["status"] = "false-positive"
        assert findings.gate_row(ledger, "abc1234")["verdict"] == "GO"

    def test_gate_lists_verified_findings_without_a_lifecycle_test(self) -> None:
        ledger = {"sha": None, "findings": []}
        findings.add(ledger, [_finding(status="verified")])
        assert findings.gate_row(ledger, "abc1234")["findings_without_lifecycle_test"] == ["RA-001"]
        ledger["findings"][0]["lifecycle_test"] = "tests/lifecycle/test_x.py::test_y"
        assert findings.gate_row(ledger, "abc1234")["findings_without_lifecycle_test"] == []

    def test_recheck_pins_verdicts_to_the_new_sha(self) -> None:
        ledger = {"sha": "old", "findings": []}
        findings.add(ledger, [_finding(severity="blocker", status="verified")])
        result = findings.recheck(
            ledger, [{"id": "RA-001", "verdict": "VERIFIED-FIXED", "evidence": "rc 0"}], "new"
        )
        assert result["table"][0]["verdict"] == "VERIFIED-FIXED"
        assert ledger["findings"][0]["status"] == "fixed"
        assert ledger["findings"][0]["history"] == [
            {"sha": "new", "verdict": "VERIFIED-FIXED", "evidence": "rc 0"}
        ]
        assert findings.gate_row(ledger, "new")["verdict"] == "GO"

    def test_recheck_rejects_unknown_ids_and_verdicts(self) -> None:
        ledger = {"sha": None, "findings": []}
        findings.add(ledger, [_finding()])
        with pytest.raises(findings.LedgerError, match="unknown finding"):
            findings.recheck(ledger, [{"id": "RA-999", "verdict": "STILL-OPEN"}], "new")
        with pytest.raises(findings.LedgerError, match="verdict"):
            findings.recheck(ledger, [{"id": "RA-001", "verdict": "MAYBE"}], "new")

    def test_coverage_counts_per_journey(self) -> None:
        rows = [
            {"step": "J01.1", "status": "pass"},
            {"step": "J01.2", "status": "fail(RA-001)"},
            {"step": "E01.1", "status": "not-covered"},
        ]
        result = findings.coverage(rows)
        assert result["per_journey"]["J01"]["fail"] == 1
        assert result["probed_ratio"] == round(2 / 3, 3)

    def test_coverage_rejects_unknown_statuses(self) -> None:
        with pytest.raises(findings.LedgerError, match="coverage status"):
            findings.coverage([{"step": "J01.1", "status": "maybe"}])


class TestWorld:
    def test_world_scrubs_host_git_and_credentials(self, tmp_path: Path) -> None:
        built = world.build_world(tmp_path, "w", None, "git.example.invalid")
        host = {
            "GITHUB_TOKEN": "x",
            "GIT_ASKPASS": "/bin/askpass",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "commit.gpgsign",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_AUTHOR_NAME": "Host",
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
        }
        env = world.scrub_env(host, built)
        assert not {
            "GITHUB_TOKEN",
            "GIT_ASKPASS",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_AUTHOR_NAME",
        } & set(env)
        assert (env["GIT_CONFIG_COUNT"], env["LANG"], env["HOME"]) == (
            "0",
            "C.UTF-8",
            built["env"]["HOME"],
        )
        gitconfig = Path(built["env"]["GIT_CONFIG_GLOBAL"]).read_text(encoding="ascii")
        assert "gpgsign = false" in gitconfig
        assert "insteadOf = https://git.example.invalid/" in gitconfig

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlink creation needs privileges on Windows"
    )
    def test_world_puts_the_candidate_binary_first_on_path(self, tmp_path: Path) -> None:
        binary = tmp_path / "apm-candidate"
        binary.write_text("#!/bin/sh\n", encoding="ascii")
        built = world.build_world(tmp_path / "worlds", "w", str(binary), "git.example.invalid")
        assert (Path(built["bin"]) / "apm").resolve() == binary.resolve()
        assert built["env"]["PATH"].split(os.pathsep)[0] == built["bin"]


class TestBriefLint:
    def test_flags_security_wording_with_suggestions(self) -> None:
        hits = brief_lint.lint("You are a red team attacker.\nExploit the payload.\n")
        assert {(hit["word"], hit["line"]) for hit in hits} == {
            ("red team", 1),
            ("attack", 1),
            ("attacker", 1),
            ("exploit", 2),
            ("payload", 2),
        }

    def test_neutral_qa_brief_is_clean(self) -> None:
        assert (
            brief_lint.lint(
                "You are the maintainers' QA engineer verifying the release candidate.\n"
            )
            == []
        )
