"""Contract for the transport-platform-authenticated-egress guard."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from scripts.architecture_linter.models import Violation
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[3]
RULE_ID = "transport-platform-authenticated-egress"

# Each injected snippet defines a uniquely named function so its violations can
# be attributed by scope; all injections share one linter run.
_FLAGGED = {
    "src/apm_cli/utils/console.py": {
        "_egress_direct": (
            "import requests\n\ndef _egress_direct(u):\n    return requests.get(u)\n"
        ),
        "_egress_alias": (
            "import requests as rq\n\ndef _egress_alias():\n    post = rq.post\n    return post\n"
        ),
        "_egress_session": (
            "def _egress_session(self, u):\n    return self._http_session.get(u)\n"
        ),
    },
    "src/apm_cli/utils/paths.py": {
        "_egress_urlopen": (
            "from urllib.request import urlopen as fetch\n\n"
            "def _egress_urlopen(u):\n    return fetch(u)\n"
        ),
        "_egress_subclass": (
            "import requests\n\nclass _egress_subclass(requests.Session):\n    pass\n"
        ),
    },
    "src/apm_cli/utils/yaml_io.py": {
        "_header_lower": (
            'def _header_lower(h, t):\n    h["authorization"] = f"token {t}"\n    return h\n'
        ),
        "_header_add": ('def _header_add(r, t):\n    r.add_header("PRIVATE-TOKEN", t)\n'),
        "_header_kwarg": ("def _header_kwarg(t):\n    return dict(Authorization=t)\n"),
        "_header_dict": ('def _header_dict(t):\n    return {"Authorization": t}\n'),
    },
}

_ALLOWED = {
    "src/apm_cli/utils/console.py": (
        "def _ok_dict_get(self, k):\n    return self._session_owners.get(k)\n\n"
        'def _ok_header_read(h):\n    return h.get("Authorization")\n'
    ),
    "src/apm_cli/utils/http_credentials.py": (
        'def _ok_owner(u, t):\n    return requests.get(u, headers={"Authorization": t})\n'
    ),
    "src/apm_cli/core/auth.py": ('def _ok_owner_header(t):\n    return {"PRIVATE-TOKEN": t}\n'),
}


def _run(overrides: dict[str, str] | None = None) -> list[Violation]:
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides=overrides)
    assert report.failures == ()
    return [v for v in report.violations if v.rule_id == RULE_ID]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


@cache
def _injected_violations() -> tuple[Violation, ...]:
    overrides: dict[str, str] = {}
    for path, snippets in _FLAGGED.items():
        overrides[path] = _source(path) + "\n\n" + "\n\n".join(snippets.values())
    for path, snippet in _ALLOWED.items():
        overrides[path] = overrides.get(path, _source(path)) + "\n\n" + snippet
    return tuple(_run(overrides))


def test_head_is_clean() -> None:
    assert _run() == []


@pytest.mark.parametrize(
    ("path", "scope"),
    [(path, scope) for path, snippets in _FLAGGED.items() for scope in snippets],
)
def test_unowned_egress_and_header_keys_are_flagged(path: str, scope: str) -> None:
    assert any(v.path == path and f" {scope}" in v.message for v in _injected_violations())


def test_owners_reads_and_non_session_receivers_are_not_flagged() -> None:
    flagged_scopes = {scope for snippets in _FLAGGED.values() for scope in snippets}
    unexpected = [
        v for v in _injected_violations() if not any(f" {s}" in v.message for s in flagged_scopes)
    ]
    assert unexpected == []


def test_retired_exemption_is_reported_stale() -> None:
    path = "src/apm_cli/utils/archive.py"
    source = _source(path)
    old = "response = _ARCHIVE_SESSION.get(url, **kwargs)"
    assert source.count(old) == 1
    violations = _run({path: source.replace(old, "response = None")})
    assert [
        (v.path, "stale egress exemption for _archive_get" in v.message) for v in violations
    ] == [(path, True)]
