"""Lockstep tests pinning the documented forms of ``--policy`` / ``--policy-source``.

The forms accepted by ``discover_policy`` (the ground-truth parser in
``apm_cli.policy.discovery``) are mirrored in:

- ``apm_cli.policy._help_text.POLICY_SOURCE_FORMS_HELP`` (Python constant)
- ``apm audit --policy`` Click help (uses the constant)
- ``apm policy status --policy-source`` Click help (uses the constant)
- ``docs/src/content/docs/reference/cli-commands.md`` (manual prose)

If any of these drift, the tests below fail. See #998 for the underlying
incident that motivated this lockstep.
"""

import re
from pathlib import Path

from click.testing import CliRunner

from apm_cli.policy._help_text import POLICY_SOURCE_FORMS_HELP

# Canonical user-facing forms accepted by ``--policy`` / ``--policy-source``.
# Tokens chosen to be robust against Click's help-text reflow (no internal
# whitespace) and to uniquely identify each form.
EXPECTED_FORM_TOKENS = ("'org'", "owner/repo", "https://", "file path")

# Same set of forms, written with the markdown backtick convention used in
# the docs (the docs render Click-style single quotes as inline code).
DOCS_FORM_TOKENS = ("`org`", "`owner/repo`", "`https://`", "file path")

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_PATH = (
    REPO_ROOT
    / "docs"
    / "src"
    / "content"
    / "docs"
    / "reference"
    / "cli-commands.md"
)


def _normalize_help_output(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    Click reflows long help strings across terminal width; the constant
    can land on word boundaries that get a newline + indent inserted.
    Collapsing whitespace lets us search for canonical phrases without
    having to anticipate every wrap point.
    """
    return re.sub(r"\s+", " ", text)


def test_canonical_constant_lists_all_supported_forms():
    """The constant text mentions every form ``discover_policy`` accepts."""
    for token in EXPECTED_FORM_TOKENS:
        assert token in POLICY_SOURCE_FORMS_HELP, (
            f"POLICY_SOURCE_FORMS_HELP missing canonical form: {token!r}. "
            "If discover_policy stopped accepting this form, the change "
            "is intentional and this test should be updated. Otherwise "
            "the constant has drifted from the parser."
        )


def test_audit_policy_help_uses_canonical_constant():
    """``apm audit --help`` includes the canonical forms list."""
    from apm_cli.commands.audit import audit

    runner = CliRunner()
    result = runner.invoke(audit, ["--help"])
    assert result.exit_code == 0, result.output

    output = _normalize_help_output(result.output)
    for token in EXPECTED_FORM_TOKENS:
        assert token in output, (
            f"`apm audit --help` missing canonical form: {token!r}. The "
            "Click decorator may have stopped using "
            "POLICY_SOURCE_FORMS_HELP."
        )


def test_policy_status_help_uses_canonical_constant():
    """``apm policy status --help`` includes the canonical forms list."""
    from apm_cli.commands.policy import policy

    runner = CliRunner()
    result = runner.invoke(policy, ["status", "--help"])
    assert result.exit_code == 0, result.output

    output = _normalize_help_output(result.output)
    for token in EXPECTED_FORM_TOKENS:
        assert token in output, (
            f"`apm policy status --help` missing canonical form: {token!r}. "
            "The Click decorator may have stopped using "
            "POLICY_SOURCE_FORMS_HELP."
        )


def _bullet_starting_with(text: str, marker: str) -> str:
    """Return the bullet line that begins with ``marker`` (up to next newline).

    Used to scope assertions to a specific flag's documentation bullet
    instead of the whole docs file -- a form keyword may appear elsewhere
    in cli-commands.md (e.g. unrelated marketplace examples), so a global
    count is not strict enough to catch a removal from the bullet we
    actually care about.
    """
    idx = text.find(marker)
    if idx < 0:
        raise AssertionError(
            f"Could not find bullet starting with {marker!r} in {DOCS_PATH.name}"
        )
    end = text.find("\n", idx)
    return text[idx:end] if end >= 0 else text[idx:]


def test_docs_audit_policy_bullet_lists_all_forms():
    """The ``apm audit --policy SOURCE`` doc bullet lists every canonical form."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    bullet = _bullet_starting_with(text, "- `--policy SOURCE`")
    for token in DOCS_FORM_TOKENS:
        assert token in bullet, (
            f"`apm audit --policy SOURCE` doc bullet missing form: {token!r}.\n"
            f"Bullet text:\n  {bullet}"
        )


def test_docs_policy_status_bullet_lists_all_forms():
    """The ``apm policy status --policy-source SOURCE`` bullet lists every canonical form."""
    text = DOCS_PATH.read_text(encoding="utf-8")
    bullet = _bullet_starting_with(text, "- `--policy-source SOURCE`")
    for token in DOCS_FORM_TOKENS:
        assert token in bullet, (
            f"`apm policy status --policy-source SOURCE` doc bullet missing form: {token!r}.\n"
            f"Bullet text:\n  {bullet}"
        )


def test_no_broken_install_policy_cross_reference_anywhere_in_docs():
    """Regression guard for #994: no doc page may reference ``apm install --policy``.

    ``apm install`` has no ``--policy`` flag (only ``--no-policy``). Any
    cross-reference to ``apm install --policy`` is a broken pointer.
    Scoped to the entire ``docs/`` tree (not just cli-commands.md) so a
    future copy-paste into another docs page is also caught.
    """
    docs_root = REPO_ROOT / "docs"
    offenders = []
    for md_path in docs_root.rglob("*.md"):
        if "apm install --policy" in md_path.read_text(encoding="utf-8"):
            offenders.append(md_path.relative_to(REPO_ROOT))
    assert not offenders, (
        "Found broken reference to `apm install --policy` (no such flag) "
        f"in: {[str(p) for p in offenders]}. See #994."
    )
