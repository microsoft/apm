"""Shared checks for catalog remediation blocks in ``core/remediation.py`` (#4090).

Each worker area keeps its catalog entries between ``# -- <block> --`` and
``# -- end <block> --`` markers. :func:`block_ids` lists the ids in one block;
:func:`assert_component_proof` is the component-level proof that an entry's
command resolves in the real Click tree and that it names the state it clears.
"""

from __future__ import annotations

import re
from pathlib import Path

from apm_cli.core.remediation import Remediation, catalog
from tests.utils.click_tree import resolve_line

OWNER = Path(__file__).resolve().parents[2] / "src" / "apm_cli" / "core" / "remediation.py"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def block_ids(block: str) -> list[str]:
    """Return the catalog ids declared between the markers of *block*."""
    text = OWNER.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"# -- {re.escape(block)} -+\n(?P<body>.*?)# -- end {re.escape(block)} -+", re.S
    )
    match = pattern.search(text)
    assert match, f"catalog block markers for {block!r} missing from core/remediation.py"
    return re.findall(r'_entry\(\s*"([a-z0-9-]+)"', match.group("body"))


def sample(text: str) -> str:
    """Fill ``{bound}`` and ``<user-supplied>`` placeholders with a sample value."""
    return re.sub(r"<[^>\s]+>", "sample", _PLACEHOLDER.sub("sample", text))


def assert_component_proof(remediation_id: str) -> Remediation:
    """The command resolves to a real command with valid flags; the promise names a state."""
    entry = catalog()[remediation_id]
    resolution = resolve_line(sample(entry.command))
    assert resolution.ok, (entry.command, resolution)
    promise = entry.fixes_promise
    assert promise and promise == promise.strip()
    assert promise[0].islower() and not promise.startswith(("to ", "run ")), promise
    assert promise.isascii() and not promise.endswith(".")
    names = set(_PLACEHOLDER.findall(repr(entry)))
    rendered = entry.bind(**dict.fromkeys(names, "sample"))
    assert f"'{rendered.command}' to {rendered.fixes_promise}" in rendered.sentence()
    return entry


def assert_proof_levels(block: str, executed: frozenset[str], component_only: dict) -> None:
    """Every id in *block* is executed end to end or component-only with a reason."""
    ids = set(block_ids(block))
    assert executed.isdisjoint(component_only)
    assert all(component_only.values()), "every component-only id needs a reason"
    assert ids == executed | set(component_only), (
        sorted(ids - executed - set(component_only)),
        sorted((executed | set(component_only)) - ids),
    )
