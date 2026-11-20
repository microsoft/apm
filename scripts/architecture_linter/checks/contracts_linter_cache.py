"""Architecture-linter session cache authority (owner: architecture-linter-performance).

Mutation cases reuse one inventory, one registry validation and one parse per
file content through :mod:`scripts.architecture_linter.session_cache`. Two
closed-world guards keep that reuse verdict-neutral:

* content key -- facts are keyed by the source content hash and the linter's
  own source fingerprint, and every run
  obtains its inventory from the session instead of walking the tree itself;
* parse once -- the facts provider consults the session before building and
  counts every build, so a second parse of the same content is observable.
"""

from __future__ import annotations

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import CallFact, Rule, Violation

CONTENT_KEY_RULE_ID = "contracts-tooling-linter-cache-content-key"
PARSE_ONCE_RULE_ID = "contracts-tooling-linter-cache-parse-once"

_SESSION = "scripts/architecture_linter/session_cache.py"
_FACTS = "scripts/architecture_linter/facts.py"
_RUNNER = "scripts/architecture_linter/runner.py"


def _calls_in(calls: tuple[CallFact, ...], scope: str, qualname: str) -> tuple[CallFact, ...]:
    return tuple(call for call in calls if call.scope == scope and call.qualname == qualname)


def _check_content_key(provider: FactsProvider) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    session, failures = checked_facts(provider, _SESSION, CONTENT_KEY_RULE_ID, require_python=True)
    findings.extend(failures)
    if not failures:
        if not _calls_in(session.calls, "facts_key", "content_digest"):
            findings.append(
                violation(
                    CONTENT_KEY_RULE_ID,
                    _SESSION,
                    "LinterSession.facts_key must key facts by content_digest(text)",
                )
            )
        if not _calls_in(session.calls, "__init__", "linter_source_digest"):
            findings.append(
                violation(
                    CONTENT_KEY_RULE_ID,
                    _SESSION,
                    "LinterSession must fingerprint the linter source with linter_source_digest()",
                )
            )
    runner, failures = checked_facts(provider, _RUNNER, CONTENT_KEY_RULE_ID, require_python=True)
    findings.extend(failures)
    if not failures:
        if not _calls_in(runner.calls, "_run", "current_session"):
            findings.append(
                violation(
                    CONTENT_KEY_RULE_ID,
                    _RUNNER,
                    "_run must obtain its LinterSession from current_session()",
                )
            )
        for call in runner.calls:
            if call.qualname.split(".")[-1] == "build_inventory":
                findings.append(
                    violation(
                        CONTENT_KEY_RULE_ID,
                        _RUNNER,
                        "runner must take its inventory from the session, not build_inventory",
                        line=call.line,
                        column=call.column + 1,
                    )
                )
    return tuple(findings)


def _check_parse_once(provider: FactsProvider) -> tuple[Violation, ...]:
    facts, failures = checked_facts(provider, _FACTS, PARSE_ONCE_RULE_ID, require_python=True)
    if failures:
        return failures
    lookups = _calls_in(facts.calls, "file_facts", "self.session.lookup_facts")
    builds = _calls_in(facts.calls, "file_facts", "_build_file_facts")
    records = _calls_in(facts.calls, "file_facts", "self.session.record_build")
    findings: list[Violation] = []
    if len(builds) != 1:
        findings.append(
            violation(
                PARSE_ONCE_RULE_ID,
                _FACTS,
                "FactsProvider.file_facts must build facts at exactly one call site",
            )
        )
    elif not lookups or min(call.line for call in lookups) > builds[0].line:
        findings.append(
            violation(
                PARSE_ONCE_RULE_ID,
                _FACTS,
                "FactsProvider.file_facts must consult session.lookup_facts before building",
                line=builds[0].line,
            )
        )
    if not records:
        findings.append(
            violation(
                PARSE_ONCE_RULE_ID,
                _FACTS,
                "FactsProvider.file_facts must count every build with session.record_build",
            )
        )
    return tuple(findings)


RULES = (
    Rule(
        CONTENT_KEY_RULE_ID,
        "contracts_tests",
        (CONTENT_KEY_RULE_ID,),
        "Linter session facts are keyed by content and runs take the session inventory.",
        _check_content_key,
    ),
    Rule(
        PARSE_ONCE_RULE_ID,
        "contracts_tests",
        (PARSE_ONCE_RULE_ID,),
        "The facts provider consults the linter session before every counted build.",
        _check_parse_once,
    ),
)
