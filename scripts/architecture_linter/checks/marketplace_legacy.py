"""Legacy bundle-format, marketplace-parsing, and LF-writer authority analyzers.

These are faithful, facts-only ports of three legacy enforcement units that had
no successor rule after the shell linter was replaced:

* ``scripts/check_bundle_format_authority.sh`` subchecks **B1-B5, B8, B9, B17,
  B18** -- the ``bundle/formats.py`` authority, the ``PREFERRED_PLUGIN_FORMAT``
  freeze, the ``"plugin"`` token stability pin, the selector seam, the
  ``--plugin`` option ban, reproducible-archive streaming, the ``init.py``
  scaffolding seam, the marketplace-resolver schema-admission ban, and the
  ``commands/install.py`` native-boundary ordering.
* legacy **AC10** -- ``_dependency_reference_from_packed_source`` and
  ``_entry_coordinates`` must parse marketplace sources through
  ``DependencyReference`` instead of a parallel URL parser.
* legacy **AC34** / ``scripts/check_hash_visible_lf_writes.py`` -- generated
  files inside hashed package trees must route through ``write_text_lf``.

Every analyzer reads source exclusively through the shared
:class:`~scripts.architecture_linter.facts.FactsProvider` (cached lexical lines
and the facts produced by the one shared AST traversal). Nothing here opens a
file, walks the filesystem, re-parses source, calls ``ast.parse`` / ``ast.walk``
/ ``ast.NodeVisitor``, or shells out to a helper.

Divergence from the legacy shell, deliberate and documented: the legacy helper
wrapped most blocks in ``[ -f ... ]`` guards, so an absent file silently skipped
its subcheck (fail-open). These analyzers fail closed through
:func:`_facts`, matching the rest of the Python linter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import (
    EXEMPT_MARKER,
    checked_facts,
    inventory_paths,
    violation,
)
from scripts.architecture_linter.models import FileFacts, Violation

_SENTINEL = "pyproject.toml"
_MODULE_SCOPE = "<module>"
_PY: tuple[str, ...] = (".py",)

# --------------------------------------------------------------------------
# Owner and consumer coordinates (legacy absolute paths, repo-relative here).
# --------------------------------------------------------------------------
_BUNDLE_PREFIX = "src/apm_cli/bundle/"
_FORMATS = "src/apm_cli/bundle/formats.py"
_ARCHIVE = "src/apm_cli/bundle/reproducible_archive.py"
_INIT_COMMAND = "src/apm_cli/commands/init.py"
_PACK_COMMAND = "src/apm_cli/commands/pack.py"
_PLUGIN_INIT_COMMAND = "src/apm_cli/commands/plugin/init.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_MARKETPLACE_RESOLVER = "src/apm_cli/marketplace/resolver.py"
_MARKETPLACE_CHECK = "src/apm_cli/commands/marketplace/check.py"
_PLUGIN_PARSER = "src/apm_cli/deps/plugin_parser.py"
_YAML_IO = "src/apm_cli/utils/yaml_io.py"


# --------------------------------------------------------------------------
# Shared, side-effect-free primitives (cached facts + inventory only).
# --------------------------------------------------------------------------
def _facts(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    path: str,
    *,
    parse: bool = False,
) -> tuple[FileFacts | None, tuple[Violation, ...]]:
    """Read one required source, failing closed on missing/unreadable/unparseable."""
    if path not in inv:
        return None, (
            violation(rule_id, _SENTINEL, f"required source missing from inventory: {path}"),
        )
    facts, failures = checked_facts(provider, path, rule_id, require_python=parse)
    if failures:
        return None, failures
    return facts, ()


def _matches(facts: FileFacts, pattern: re.Pattern[str]) -> bool:
    """Return whether any cached line matches `pattern` (``grep -Eq``)."""
    return any(pattern.search(line) is not None for line in facts.lines)


def _first_line(facts: FileFacts, pattern: re.Pattern[str]) -> int | None:
    """Return the 1-based line of the first match, else ``None`` (``grep -n``)."""
    for number, line in enumerate(facts.lines, start=1):
        if pattern.search(line) is not None:
            return number
    return None


def _require_patterns(
    facts: FileFacts,
    rule_id: str,
    path: str,
    patterns: Sequence[re.Pattern[str]],
    message: str,
) -> tuple[Violation, ...]:
    """Require every anchored pattern in one cached source file."""
    missing = [pattern.pattern for pattern in patterns if not _matches(facts, pattern)]
    if not missing:
        return ()
    return (violation(rule_id, path, f"{message}; missing pattern(s): {', '.join(missing)}"),)


def _forbid_pattern(
    facts: FileFacts,
    rule_id: str,
    path: str,
    pattern: re.Pattern[str],
    message: str,
    *,
    exempt: bool,
) -> tuple[Violation, ...]:
    """Report every forbidden line, honoring the legacy exemption marker only when asked."""
    findings: list[Violation] = []
    for number, line in enumerate(facts.lines, start=1):
        if exempt and EXEMPT_MARKER in line:
            continue
        match = pattern.search(line)
        if match is not None:
            findings.append(
                violation(rule_id, path, message, line=number, column=match.start() + 1)
            )
    return tuple(findings)


def _awk_block(
    facts: FileFacts,
    start: re.Pattern[str],
    boundary: re.Pattern[str],
    keep: re.Pattern[str] | None = None,
) -> tuple[tuple[int, str], ...]:
    """Capture a block exactly like the legacy ``awk`` body extractor.

    Mirrors ``/start/{flag=1} flag&&/boundary/&&!/keep/{exit} flag{print}``:
    capture opens on the first `start` line (inclusive) and closes just before
    the next `boundary` line that does not also match `keep` (which defaults to
    `start`, so a repeated opening signature never terminates the block).
    """
    keep_pattern = keep if keep is not None else start
    body: list[tuple[int, str]] = []
    capturing = False
    for number, line in enumerate(facts.lines, start=1):
        if not capturing:
            if start.search(line) is None:
                continue
            capturing = True
        elif boundary.search(line) is not None and keep_pattern.search(line) is None:
            break
        body.append((number, line))
    return tuple(body)


def _block_missing(block: Sequence[tuple[int, str]], needles: Sequence[str]) -> list[str]:
    """Return the literal fragments absent from a captured block (``grep -Fq``)."""
    return [needle for needle in needles if not any(needle in line for _, line in block)]


def _block_hits(
    block: Sequence[tuple[int, str]],
    pattern: re.Pattern[str],
    *,
    ignore: Sequence[str] = (),
) -> tuple[tuple[int, str], ...]:
    """Return block lines matching `pattern`, minus the legacy ``grep -v`` filters."""
    skip = (*ignore, EXEMPT_MARKER)
    return tuple(
        (number, line)
        for number, line in block
        if pattern.search(line) is not None and not any(token in line for token in skip)
    )


def _terminal_call_names(facts: FileFacts, low: int, high: int) -> list[str]:
    """Terminal callee names for every call inside the inclusive line span.

    Reproduces the helper's ``_call_name`` over ``ast.walk(function)``: the
    shared traversal already recorded every call in the file, and a function's
    subtree is exactly the calls whose line falls inside its definition span
    (nested definitions included, exactly as ``ast.walk`` includes them).
    """
    return [call.qualname.rsplit(".", 1)[-1] for call in facts.calls if low <= call.line <= high]


def _sole_module_function(facts: FileFacts, name: str) -> tuple[int, int] | None:
    """Return the span of the one top-level ``name`` function, else ``None``."""
    spans = [
        (definition.line, definition.end_line)
        for definition in facts.definitions
        if definition.name == name
        and definition.scope == _MODULE_SCOPE
        and definition.kind in ("function", "async_function")
    ]
    return spans[0] if len(spans) == 1 else None


def _python_paths(provider: FactsProvider, prefix: str) -> tuple[str, ...]:
    """Inventory ``*.py`` paths under `prefix` (prefix AND suffix, not union)."""
    return tuple(
        path for path in inventory_paths(provider, prefixes=(prefix,)) if path.endswith(_PY)
    )


# --------------------------------------------------------------------------
# B1-B5, B8, B9, B17, B18 -- check_bundle_format_authority.sh.
# --------------------------------------------------------------------------
# B1: the bundle-format authority may not be redefined outside formats.py.
_FORMAT_DUPLICATE = re.compile(
    r"^(class BundleFormat|def resolve_bundle_format|def agent_plugin_warning)"
)
# B2/B3/B4: exact, anchored pins inside the format owner.
_PREFERRED_PIN = re.compile(r"^PREFERRED_PLUGIN_FORMAT = BundleFormat\.CLAUDE_PLUGIN$")
_PLUGIN_TOKEN_PIN = re.compile(r"^    \"plugin\": BundleFormat\.CLAUDE_PLUGIN,$")
_SELECTOR_SEAM = re.compile(r"^    if len\(selections\) > 1:$")
_NO_FLAG_SEAM = re.compile(r"^    return PREFERRED_PLUGIN_FORMAT$")
# B5: the retired --plugin switch may not come back on either command.
_PLUGIN_OPTION = re.compile(r"^\s*([\"']--plugin[\"'],|@click\.option\([\"']--plugin[\"'])")
# B8: reproducible archives stream; they never buffer a whole member.
_ARCHIVE_BUFFERING = (".read_bytes(", "source.read(", "BytesIO")
_ARCHIVE_STREAMING = ("shutil.copyfileobj(source, member)", "archive.addfile(info, source)")
# B9: plugin scaffolding shares the preferred-format seam and canonical reload.
_INIT_SEAMS = (
    "PREFERRED_PLUGIN_FORMAT is BundleFormat.AGENT_PLUGIN",
    "plugin = load_agent_plugin(staged_root)",
)
# B17: marketplace resolution defers schema admission to materialized ingress.
_SCHEMA_ADMISSION = re.compile(r"route_agent_plugin_package|detect_agent_plugin|\$schema")
# B18: the native boundary runs before local-bundle deployment preparation.
_BOUNDARY_GATE = re.compile(r"enforce_agent_plugin_deployment_boundary\(bundle_info=_bundle_info\)")
_LOCAL_BUNDLE_IMPORT = re.compile(
    r"from \.\.install\.local_bundle_handler import install_local_bundle"
)
_EXECUTABLE_TRUST_IMPORT = re.compile(r"_allow_execs_for_bundle = _effective_bundle_allow_map")


def _check_format_owner(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """B1-B4: single format authority plus the three frozen seams inside it."""
    findings: list[Violation] = []

    # B1 -- duplicate scan over the whole bundle package except the owner.
    for path in _python_paths(provider, _BUNDLE_PREFIX):
        if path == _FORMATS:
            continue
        facts, failures = _facts(provider, inv, rule_id, path, parse=True)
        if failures:
            findings.extend(failures)
            continue
        findings.extend(
            _forbid_pattern(
                facts,
                rule_id,
                path,
                _FORMAT_DUPLICATE,
                "Bundle format authority must live in src/apm_cli/bundle/formats.py",
                exempt=False,
            )
        )

    owner, failures = _facts(provider, inv, rule_id, _FORMATS, parse=True)
    if failures:
        return tuple(findings) + failures

    # B2 -- the Agent Plugin preferred-default flip stays frozen for T10/G3.
    findings.extend(
        _require_patterns(
            owner,
            rule_id,
            _FORMATS,
            (_PREFERRED_PIN,),
            "Agent Plugin preferred-default flip is reserved for T10 after G3",
        )
    )
    # B3 -- the plugin format token stays Claude-compatible for apm-action@v1.
    findings.extend(
        _require_patterns(
            owner,
            rule_id,
            _FORMATS,
            (_PLUGIN_TOKEN_PIN,),
            "The plugin format token must remain Claude-compatible for apm-action@v1",
        )
    )
    # B4 -- selectors and no-flag behavior route through the canonical seam.
    findings.extend(
        _require_patterns(
            owner,
            rule_id,
            _FORMATS,
            (_SELECTOR_SEAM, _NO_FLAG_SEAM),
            "Bundle selectors and no-flag behavior must route through the canonical format seam",
        )
    )
    return tuple(findings)


def _check_plugin_option_ban(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """B5: portable Agent Plugins use ``--format agent-plugin``, never ``--plugin``."""
    findings: list[Violation] = []
    for path in (_PACK_COMMAND, _PLUGIN_INIT_COMMAND):
        facts, failures = _facts(provider, inv, rule_id, path, parse=True)
        if failures:
            findings.extend(failures)
            continue
        findings.extend(
            _forbid_pattern(
                facts,
                rule_id,
                path,
                _PLUGIN_OPTION,
                "Portable Agent Plugins must use --format agent-plugin, not --plugin",
                exempt=False,
            )
        )
    return tuple(findings)


def _check_reproducible_archive(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """B8: archive members stream through ``copyfileobj``; nothing buffers them."""
    facts, failures = _facts(provider, inv, rule_id, _ARCHIVE, parse=True)
    if failures:
        return failures

    message = "Reproducible archives must stream file payloads without full-file buffering"
    findings: list[Violation] = []
    for number, line in enumerate(facts.lines, start=1):
        for needle in _ARCHIVE_BUFFERING:
            column = line.find(needle)
            if column >= 0:
                findings.append(
                    violation(rule_id, _ARCHIVE, message, line=number, column=column + 1)
                )
    text = "\n".join(facts.lines)
    missing = [needle for needle in _ARCHIVE_STREAMING if needle not in text]
    if missing:
        rendered = ", ".join(repr(item) for item in missing)
        findings.append(violation(rule_id, _ARCHIVE, f"{message}; missing: {rendered}"))
    return tuple(findings)


def _check_init_scaffolding(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """B9: ``commands/init.py`` shares the preferred-format seam and canonical reload."""
    facts, failures = _facts(provider, inv, rule_id, _INIT_COMMAND, parse=True)
    if failures:
        return failures
    text = "\n".join(facts.lines)
    missing = [needle for needle in _INIT_SEAMS if needle not in text]
    if not missing:
        return ()
    rendered = ", ".join(repr(item) for item in missing)
    return (
        violation(
            rule_id,
            _INIT_COMMAND,
            "Plugin scaffolding must share the preferred-format seam and canonical reload; "
            f"missing: {rendered}",
        ),
    )


def _check_resolver_schema_admission(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """B17: marketplace resolution never admits plugin schemas itself."""
    facts, failures = _facts(provider, inv, rule_id, _MARKETPLACE_RESOLVER, parse=True)
    if failures:
        return failures
    return _forbid_pattern(
        facts,
        rule_id,
        _MARKETPLACE_RESOLVER,
        _SCHEMA_ADMISSION,
        "Marketplace resolution must defer schema admission to materialized ingress",
        exempt=False,
    )


def _check_local_bundle_boundary_order(
    provider: FactsProvider, inv: frozenset[str], rule_id: str
) -> tuple[Violation, ...]:
    """B18: the native boundary fires before deployment preparation is even imported."""
    facts, failures = _facts(provider, inv, rule_id, _INSTALL_COMMAND, parse=True)
    if failures:
        return failures

    gate = _first_line(facts, _BOUNDARY_GATE)
    handler = _first_line(facts, _LOCAL_BUNDLE_IMPORT)
    trust = _first_line(facts, _EXECUTABLE_TRUST_IMPORT)
    if gate is None or handler is None or trust is None or gate >= handler or gate >= trust:
        return (
            violation(
                rule_id,
                _INSTALL_COMMAND,
                "Local bundles must hit the native boundary before deployment preparation",
                line=gate or 1,
            ),
        )
    return ()


def check_bundle_format_authority(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """Run every ported ``check_bundle_format_authority.sh`` subcheck."""
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(_check_format_owner(provider, inv, rule_id))
    findings.extend(_check_plugin_option_ban(provider, inv, rule_id))
    findings.extend(_check_reproducible_archive(provider, inv, rule_id))
    findings.extend(_check_init_scaffolding(provider, inv, rule_id))
    findings.extend(_check_resolver_schema_admission(provider, inv, rule_id))
    findings.extend(_check_local_bundle_boundary_order(provider, inv, rule_id))
    return tuple(findings)


# --------------------------------------------------------------------------
# AC10 -- marketplace source parsing authority.
# --------------------------------------------------------------------------
_DEF_BOUNDARY = re.compile(r"^def ")
_PACKED_SOURCE_DEF = re.compile(r"^def _dependency_reference_from_packed_source\(")
_ENTRY_COORDINATES_DEF = re.compile(r"^def _entry_coordinates\(")
_PACKED_SOURCE_REQUIRED = (
    'entry: dict[str, object] = {"git": remote.strip()}',
    'entry["path"] = path',
    'entry["ref"] = declared_ref',
    "dependency = DependencyReference.parse_from_dict(entry)",
    "if dependency.is_local:",
)
_PACKED_SOURCE_PARALLEL = re.compile(r"urlparse\(|urllib\.parse|DependencyReference\(")
_PACKED_SOURCE_IGNORE = ("DependencyReference.parse_from_dict",)
_ENTRY_COORDINATES_REQUIRED = (
    "DependencyReference.parse(entry.source_url)",
    "DependencyReference.parse(source_url)",
)
_ENTRY_COORDINATES_PARALLEL = re.compile(
    r"split_source_base\(|decode_url_path_segments\(|urlparse\("
)


def _check_block_parse_authority(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    *,
    path: str,
    opener: re.Pattern[str],
    required: Sequence[str],
    parallel: re.Pattern[str],
    ignore: Sequence[str],
    message: str,
) -> tuple[Violation, ...]:
    """One legacy ``awk``-body parse-authority guard: required calls, no parallel parser."""
    facts, failures = _facts(provider, inv, rule_id, path, parse=True)
    if failures:
        return failures

    block = _awk_block(facts, opener, _DEF_BOUNDARY)
    if not block:
        return (violation(rule_id, path, f"{message}; owner body not found"),)

    findings: list[Violation] = []
    for number, _line in _block_hits(block, parallel, ignore=ignore):
        findings.append(violation(rule_id, path, message, line=number))
    missing = _block_missing(block, required)
    if missing:
        rendered = ", ".join(repr(item) for item in missing)
        findings.append(
            violation(rule_id, path, f"{message}; missing: {rendered}", line=block[0][0])
        )
    return tuple(findings)


def check_marketplace_source_parsing(
    provider: FactsProvider, rule_id: str
) -> tuple[Violation, ...]:
    """AC10: packed sources and check coordinates parse through DependencyReference."""
    inv = frozenset(provider.inventory)
    return (
        *_check_block_parse_authority(
            provider,
            inv,
            rule_id,
            path=_MARKETPLACE_RESOLVER,
            opener=_PACKED_SOURCE_DEF,
            required=_PACKED_SOURCE_REQUIRED,
            parallel=_PACKED_SOURCE_PARALLEL,
            ignore=_PACKED_SOURCE_IGNORE,
            message="Packed marketplace sources must use DependencyReference.parse_from_dict",
        ),
        *_check_block_parse_authority(
            provider,
            inv,
            rule_id,
            path=_MARKETPLACE_CHECK,
            opener=_ENTRY_COORDINATES_DEF,
            required=_ENTRY_COORDINATES_REQUIRED,
            parallel=_ENTRY_COORDINATES_PARALLEL,
            ignore=(),
            message="Marketplace check source coordinates must use DependencyReference parsing",
        ),
    )


# --------------------------------------------------------------------------
# AC34 -- hash-visible generated files use canonical LF writers.
# --------------------------------------------------------------------------
_LF_HELPER = "write_text_lf"
_DIRECT_WRITERS = frozenset({"open", "write_bytes", "write_text"})
_LF_CONTRACTS: tuple[tuple[str, str], ...] = (
    (_PLUGIN_PARSER, "synthesize_apm_yml_from_plugin"),
    (_PLUGIN_PARSER, "_map_plugin_artifacts"),
    (_YAML_IO, "dump_yaml"),
)


def check_hash_visible_lf_writers(provider: FactsProvider, rule_id: str) -> tuple[Violation, ...]:
    """AC34: every guarded hash-visible writer calls ``write_text_lf`` exactly once."""
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    for path, function in _LF_CONTRACTS:
        facts, failures = _facts(provider, inv, rule_id, path, parse=True)
        if failures:
            findings.extend(failures)
            continue
        span = _sole_module_function(facts, function)
        if span is None:
            findings.append(violation(rule_id, path, f"expected exactly one {function} definition"))
            continue
        low, high = span
        names = _terminal_call_names(facts, low, high)
        if names.count(_LF_HELPER) != 1:
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"{function} must call {_LF_HELPER} exactly once",
                    line=low,
                )
            )
        bypasses = sorted({name for name in names if name in _DIRECT_WRITERS})
        if bypasses:
            findings.append(
                violation(
                    rule_id,
                    path,
                    f"{function} bypasses canonical LF writer via {', '.join(bypasses)}",
                    line=low,
                )
            )
    return tuple(findings)


__all__ = [
    "check_bundle_format_authority",
    "check_hash_visible_lf_writers",
    "check_marketplace_source_parsing",
]
