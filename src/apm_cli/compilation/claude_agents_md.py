"""Claude Code instruction routing onto the AGENTS.md standard (#3939).

Claude Code 2.1.277+ reads ``AGENTS.md`` natively, but only while no
``CLAUDE.md`` counts, or when a ``CLAUDE.md`` imports ``@AGENTS.md``. APM
writes ``CLAUDE.md`` files that import ``@AGENTS.md``, so the shared
AGENTS.md output loads on every Claude Code version.

This module is the single owner of two decisions:

* which project instructions Claude receives through ``AGENTS.md`` rather
  than ``.claude/rules/`` (:func:`routes_to_agents_md`) and exactly which
  ``AGENTS.md`` carries a directory instruction
  (:func:`pin_directory_placements`), and
* whether the project can take that route without dropping guidance for an
  existing hand-authored root file (:func:`claude_agents_md_blocker`).

Install (``.claude/rules/`` deployment) and compile (``AGENTS.md`` content
and the ``CLAUDE.md`` import) both consult these helpers, so the two sides
cannot disagree about where an instruction lives.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..utils.path_security import PathTraversalError, ensure_path_within
from ..utils.paths import portable_relpath
from ..utils.patterns import normalize_apply_to, parse_apply_to
from .constants import (
    AGENTS_MD_GENERATED_MARKER,
    DISTRIBUTED_AGENTS_MD_GENERATED_MARKER,
    decode_utf8_prefix,
    has_generated_marker_header,
)

AGENTS_MD_IMPORT = "@AGENTS.md"

_GLOB_CHARACTERS = frozenset("*?[]{}!\\")
_IMPORT_PATTERN = re.compile(r"(?<![\w`@/.-])@(?:\./)?AGENTS\.md(?![\w-]|\.\w)")
_READ_LIMIT = 1024 * 1024


def _is_literal_directory(prefix: str) -> bool:
    segments = prefix.split("/")
    return all(
        segment not in ("", ".", "..") and not (set(segment) & _GLOB_CHARACTERS)
        for segment in segments
    )


def directory_scope(apply_to: object) -> str | None:
    """Return ``dir`` when ``applyTo`` is exactly one literal ``dir/**`` glob."""
    globs = parse_apply_to(normalize_apply_to(apply_to, default=""))
    if len(globs) != 1:
        return None
    pattern = str(globs[0]).strip()
    for suffix in ("/**/*", "/**"):
        if pattern.endswith(suffix):
            prefix = pattern[: -len(suffix)]
            return prefix if _is_literal_directory(prefix) else None
    return None


def _is_always_on(apply_to: object) -> bool:
    globs = parse_apply_to(normalize_apply_to(apply_to, default=""))
    return not globs or (len(globs) == 1 and str(globs[0]).strip() in ("**", "**/*"))


def routes_to_agents_md(
    apply_to: object, project_root: Path | None = None, *, cache: dict | None = None
) -> bool:
    """Return True when Claude should load this instruction from ``AGENTS.md``.

    ``applyTo: "**"`` (or no ``applyTo``) is always-on guidance for the root
    ``AGENTS.md``. One literal directory prefix such as ``src/test/**`` is
    directory guidance for exactly ``src/test/AGENTS.md``. Every other
    pattern -- file-type globs, wildcard directories, or lists -- stays a
    path-scoped ``.claude/rules/`` file so its scope is not widened.

    *cache* (one dict per install) keeps each directory's verdict, so many
    instructions sharing a directory read its files once.

    With *project_root*, a directory instruction routes only when the project
    compiles distributed output (a single-file ``AGENTS.md`` would load it for
    every file) and compile can deliver ``<dir>/AGENTS.md`` to Claude: the
    directory exists outside any nested Git repository, and no hand-authored
    or symlinked ``AGENTS.md`` or import-less ``CLAUDE.md`` sits in it.
    """
    if _is_always_on(apply_to):
        return True
    prefix = directory_scope(apply_to)
    if prefix is None:
        return False
    if project_root is None:
        return True
    key = ("directory", project_root, prefix)
    if cache is not None and key in cache:
        return cache[key]
    routed = not _single_file_compile(project_root) and _directory_destination_open(
        project_root, prefix
    )
    if cache is not None:
        cache[key] = routed
    return routed


def imports_agents_md(content: str) -> bool:
    """Return True when Claude memory text imports the sibling ``AGENTS.md``.

    Claude Code skips imports inside fenced code blocks and code spans, so
    those mentions do not count.
    """
    in_fence = False
    for line in content.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence and _IMPORT_PATTERN.search(re.sub(r"`[^`]*`", "", line)):
            return True
    return False


def _read_text(path: Path, project_root: Path) -> str | None:
    try:
        ensure_path_within(path, project_root)
        with path.open("rb") as handle:
            return decode_utf8_prefix(handle.read(_READ_LIMIT))
    except (OSError, PathTraversalError, UnicodeDecodeError):
        return None


def claude_md_imports_agents_md(path: Path, project_root: Path) -> bool:
    """Return True when the CLAUDE.md at *path* imports its sibling AGENTS.md."""
    content = _read_text(path, project_root)
    return content is not None and imports_agents_md(content)


def user_claude_md_imports_agents_md(path: Path, project_root: Path) -> bool:
    """Return True for a hand-authored CLAUDE.md that already imports AGENTS.md.

    That file already gives Claude the AGENTS.md output, so compile leaves it
    untouched without a protection warning.
    """
    from .claude_formatter import CLAUDE_HEADER

    if path.is_symlink() or not path.is_file():
        return False
    content = _read_text(path, project_root)
    return (
        content is not None
        and not has_generated_marker_header(content, (CLAUDE_HEADER,))
        and imports_agents_md(content)
    )


def _compilation_config(project_root: Path) -> dict:
    from ..core.scope import manifest_file
    from ..utils.yaml_io import load_yaml

    manifest = manifest_file(project_root)
    if not manifest.is_file():
        return {}
    try:
        data = load_yaml(manifest) or {}
    except Exception:
        return {}
    compilation = data.get("compilation") if isinstance(data, dict) else None
    return compilation if isinstance(compilation, dict) else {}


def _single_file_compile(project_root: Path) -> bool:
    compilation = _compilation_config(project_root)
    return compilation.get("strategy") == "single-file" or compilation.get("single_file") is True


def _agents_md_managed_section(project_root: Path) -> bool:
    agents_md = _compilation_config(project_root).get("agents_md")
    return isinstance(agents_md, dict) and agents_md.get("mode") == "managed_section"


def _managed_section_markers(project_root: Path) -> tuple[str, str]:
    """The managed-section markers compile uses (``apm.yml`` overrides the defaults)."""
    agents_md = _compilation_config(project_root).get("agents_md")
    agents_md = agents_md if isinstance(agents_md, dict) else {}
    start = agents_md.get("start_marker")
    end = agents_md.get("end_marker")
    return (
        start if isinstance(start, str) else "<!-- apm:start -->",
        end if isinstance(end, str) else "<!-- apm:end -->",
    )


def _managed_section_blocker(content: str, label: str, project_root: Path) -> str | None:
    """Why compile cannot write the managed section of a hand-authored file, or None."""
    from .managed_section import ManagedSectionError, validate_managed_section

    start, end = _managed_section_markers(project_root)
    try:
        validate_managed_section(content, start, end)
    except ManagedSectionError:
        return (
            f"{label} has no single {start} ... {end} managed section;"
            " add each marker once, start first"
        )
    return None


def _label(directory: Path, project_root: Path, name: str) -> str:
    """Name *name* in *directory* relative to the project, without following links."""
    relative = portable_relpath(directory, project_root)
    return name if relative == "." else f"{relative}/{name}"


def _claude_md_blocker(directory: Path, project_root: Path) -> str | None:
    """Why the ``CLAUDE.md`` in *directory* keeps Claude from reading its ``AGENTS.md``."""
    from .claude_formatter import CLAUDE_HEADER

    claude_md = directory / "CLAUDE.md"
    agents_md = directory / "AGENTS.md"
    if not (claude_md.is_symlink() or claude_md.exists()):
        return None
    label = _label(directory, project_root, "CLAUDE.md")
    content = _read_text(claude_md, project_root)
    try:
        same_file = agents_md.exists() and claude_md.samefile(agents_md)
    except OSError:
        same_file = False
    if same_file:
        return None
    if content is None:
        return f"{label} could not be read; make it readable"
    generated = not claude_md.is_symlink() and has_generated_marker_header(
        content, (CLAUDE_HEADER,)
    )
    if not generated and not imports_agents_md(content):
        return f"{label} is hand-authored and does not import @AGENTS.md; add '@AGENTS.md' to it"
    return None


def _agents_md_blocker(directory: Path, project_root: Path) -> str | None:
    """Why compile cannot write the ``AGENTS.md`` in *directory*, or None."""
    agents_md = directory / "AGENTS.md"
    if not (agents_md.is_symlink() or agents_md.exists()):
        return None
    label = _label(directory, project_root, "AGENTS.md")
    if agents_md.is_symlink():
        # Compile never writes through a symlinked AGENTS.md, managed section or not.
        return f"{label} is a symlink; replace it with a regular file"
    content = _read_text(agents_md, project_root)
    generated = content is not None and has_generated_marker_header(
        content, (AGENTS_MD_GENERATED_MARKER, DISTRIBUTED_AGENTS_MD_GENERATED_MARKER)
    )
    if not _agents_md_managed_section(project_root):
        if generated:
            return None
        return (
            f"{label} is hand-authored and APM does not manage a section in it;"
            " set 'compilation.agents_md.mode: managed_section' in apm.yml"
        )
    # Managed-section mode writes only between the markers, generated file or not.
    if content is None:
        return f"{label} could not be read; make it readable"
    return _managed_section_blocker(content, label, project_root)


def _in_nested_repository(directory: Path, project_root: Path) -> bool:
    """Return True when *directory* sits in a nested Git repository compile skips."""
    current = directory
    while current != project_root and project_root in current.parents:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def claude_agents_md_blocker(project_root: Path, cache: dict | None = None) -> str | None:
    """Return why Claude cannot take the AGENTS.md route here, or None.

    Existing user files block the route, so instructions stay in
    ``.claude/rules/`` and nothing the user wrote is overwritten:

    * a hand-authored root ``CLAUDE.md`` that does not import ``@AGENTS.md``
      (Claude Code then never reads ``AGENTS.md``),
    * a hand-authored root ``AGENTS.md`` APM may not write into (no
      ``compilation.agents_md.mode: managed_section``, or no single pair of
      its managed-section markers), and
    * a symlinked root ``AGENTS.md``, which compile never writes through.
    """
    key = ("blocker", project_root)
    if cache is not None and key in cache:
        return cache[key]
    blocker = _claude_md_blocker(project_root, project_root) or _agents_md_blocker(
        project_root, project_root
    )
    if cache is not None:
        cache[key] = blocker
    return blocker


def _directory_destination_open(project_root: Path, prefix: str) -> bool:
    """Return True when compile can deliver ``<prefix>/AGENTS.md`` to Claude.

    The same files that block the root route keep a directory instruction in
    ``.claude/rules/``: a nested ``AGENTS.md`` compile may not write, a nested
    ``CLAUDE.md`` without the import, a destination in a nested Git
    repository that compile skips, or one a symlink resolves outside the
    project (compile refuses to write there).
    """
    directory = project_root / prefix
    if not directory.is_dir() or _in_nested_repository(directory, project_root):
        return False
    try:
        ensure_path_within(directory, project_root)
    except PathTraversalError:
        return False
    return (
        _agents_md_blocker(directory, project_root) is None
        and _claude_md_blocker(directory, project_root) is None
    )


def claude_agents_md_blocker_warning(blocker: str) -> str:
    """Render the warning for *blocker* from the remediation catalog."""
    from ..core.remediation import CLAUDE_AGENTS_MD_BLOCKED

    return (
        f"{CLAUDE_AGENTS_MD_BLOCKED.bind(fix=blocker).sentence()} Always-on and directory "
        "instructions stay in .claude/rules/ until a compiled AGENTS.md carries them."
    )


def is_claude_only_target(target: object) -> bool:
    """Return True when Claude Code is the only consumer of compiled AGENTS.md."""
    if isinstance(target, frozenset):
        return target == frozenset({"claude"})
    if not isinstance(target, str):
        return False
    from ..core.target_catalog import get_target_capability

    try:
        return get_target_capability(target).compile_family == "claude"
    except KeyError:
        return False


def _rule_has_scope(rule: Path, base_dir: Path, apply_to: object) -> bool:
    """Return True when the rule at *rule* was rendered with *apply_to*'s scope.

    Install writes ``applyTo`` as the rule's ``paths:`` list, so a rule
    another source deployed under the same filename (a different scope) does
    not deliver this instruction; a kept legacy rule, edited or not, does.
    """
    from ..utils.yaml_io import loads_frontmatter

    content = _read_text(rule, base_dir)
    if content is None:
        return False
    try:
        metadata = loads_frontmatter(content).metadata
    except Exception:
        return False
    paths = metadata.get("paths") if isinstance(metadata, dict) else None
    if isinstance(paths, str):
        paths = [paths]
    if paths is not None and not isinstance(paths, list):
        return False
    expected = parse_apply_to(normalize_apply_to(apply_to, default=""))
    return [str(path).strip() for path in paths or ()] == [str(glob).strip() for glob in expected]


def without_claude_rule_instructions(primitives, base_dir: Path, warn_fn):
    """Return *primitives* minus instructions ``.claude/rules/`` delivers.

    Used when Claude Code is the only AGENTS.md consumer: path-scoped
    instructions ``apm install`` deployed as rules must not load twice. Any
    instruction without a deployed rule of its own scope (always-on,
    directory-scoped, a compile-only project, or a same-named rule another
    source deployed) stays, so nothing is dropped.
    """
    import copy

    from .root_context_protection import (
        _build_expected_rule_filenames,
        _get_deployed_instruction_filenames,
        _instruction_rule_filename,
    )

    deployed = _get_deployed_instruction_filenames(
        base_dir / ".claude" / "rules",
        base_dir,
        warn_fn,
        expected_filenames=_build_expected_rule_filenames("claude", primitives),
    )
    if not deployed:
        return primitives
    filtered = copy.copy(primitives)
    rules_dir = base_dir / ".claude" / "rules"
    filtered.instructions = [
        instruction
        for instruction in primitives.instructions
        if (name := _instruction_rule_filename("claude", instruction)) not in deployed
        or not _rule_has_scope(rules_dir / name, base_dir, instruction.apply_to)
    ]
    return filtered


def pin_directory_placements(
    placement_map: dict, base_dir: Path, warn_fn, source_dir: Path | None = None
) -> dict:
    """Place each directory instruction in exactly ``<dir>/AGENTS.md``.

    The shared optimizer may hoist a widely matching glob to the root or fall
    back to a parent; for Claude that would load directory guidance in every
    session. A directory that does not exist in the source project
    (*source_dir*, which ``apm compile --root`` separates from the output
    *base_dir*) gets no file and a warning.
    """
    pinned: dict[Path, list] = {}
    moved: list = []
    for directory, instructions in placement_map.items():
        for instruction in instructions:
            if directory_scope(instruction.apply_to) is None:
                pinned.setdefault(directory, []).append(instruction)
            elif instruction not in moved:
                moved.append(instruction)
    for instruction in moved:
        prefix = directory_scope(instruction.apply_to)
        if ((source_dir or base_dir) / prefix).is_dir():
            pinned.setdefault(base_dir / prefix, []).append(instruction)
        else:
            warn_fn(
                f"applyTo '{instruction.apply_to}' names no directory; "
                f"{instruction.file_path.name} is not written to AGENTS.md"
            )
    return pinned


def without_directory_instructions(primitives, warn_fn):
    """Keep directory instructions out of a single-file ``AGENTS.md``.

    A single root file would load them for every file. Declaring
    ``compilation.strategy: single-file`` keeps them in ``.claude/rules/``.
    """
    import copy

    from ..core.remediation import CLAUDE_DIRECTORY_INSTRUCTION_SINGLE_FILE

    kept = [i for i in primitives.instructions if directory_scope(i.apply_to) is None]
    if len(kept) == len(primitives.instructions):
        return primitives
    for instruction in primitives.instructions:
        if directory_scope(instruction.apply_to) is not None:
            warn_fn(
                f"{instruction.file_path.name} applies to one directory and is not written "
                f"to a single-file AGENTS.md; {CLAUDE_DIRECTORY_INSTRUCTION_SINGLE_FILE.clause()}"
            )
    filtered = copy.copy(primitives)
    filtered.instructions = kept
    return filtered


def _claude_loads_agents_md(directory: Path, project_root: Path) -> bool:
    """Return True when the ``CLAUDE.md`` in *directory* gives Claude its ``AGENTS.md``."""
    claude_md = directory / "CLAUDE.md"
    try:
        if claude_md.exists() and claude_md.samefile(directory / "AGENTS.md"):
            return True
    except OSError:
        return False
    return claude_md_imports_agents_md(claude_md, project_root)


def instruction_fingerprint(apply_to: object, body: str) -> str:
    """Exact identity of a routed instruction: its scope plus its normalized body.

    Compile records it for each instruction it writes to a Claude ``AGENTS.md``;
    install compares it, so an ``applyTo`` or body edit after compiling asks
    for ``apm compile`` again, while compile's link rewriting does not.
    """
    scope = normalize_apply_to(apply_to, default="")
    text = body.replace("\r\n", "\n").strip()
    encoded = json.dumps([scope, text], ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def record_compiled_instructions(base_dir: Path, written: dict) -> None:
    """Record which routed instructions a Claude compile wrote to each ``AGENTS.md``.

    *written* maps each ``AGENTS.md`` this compile wrote to the instructions it
    carries. The record (APM home, keyed by the output root) also pins each
    file's bytes, so a later rewrite of that file by anything else is stale.
    """
    from ..core.agents_md_compile_record import write_record

    files = {}
    for agents_md, instructions in sorted(written.items()):
        digest = _file_digest(agents_md)
        if digest is None:
            continue
        files[portable_relpath(agents_md, base_dir)] = {
            "sha256": digest,
            "instructions": sorted(
                {instruction_fingerprint(i.apply_to, i.content) for i in instructions}
            ),
        }
    write_record(base_dir, files)


class CompiledCoverage:
    """Whether a compiled ``AGENTS.md`` Claude loads carries a routed instruction.

    One instance serves one install: the compile record, each file's digest
    and each directory's ``CLAUDE.md`` import check are read once per path.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._index: dict[str, list[Path]] | None = None
        self._current: dict[Path, bool] = {}
        self._loads: dict[Path, bool] = {}
        self._texts: dict[Path, str | None] = {}

    def _fingerprint_index(self) -> dict[str, list[Path]]:
        """Map each recorded fingerprint to the compiled files that carry it."""
        if self._index is None:
            from ..core.agents_md_compile_record import read_record

            self._index = {}
            for relative, entry in read_record(self.project_root).items():
                if not isinstance(entry, dict) or not isinstance(relative, str):
                    continue
                agents_md = self.project_root / relative
                self._current[agents_md] = agents_md.is_file() and _file_digest(
                    agents_md
                ) == entry.get("sha256")
                for fingerprint in entry.get("instructions") or ():
                    self._index.setdefault(fingerprint, []).append(agents_md)
        return self._index

    def _claude_loads(self, directory: Path) -> bool:
        if directory not in self._loads:
            self._loads[directory] = _claude_loads_agents_md(directory, self.project_root)
        return self._loads[directory]

    def has(self, apply_to: object, body: str) -> bool:
        """Return True when a compiled ``AGENTS.md`` Claude loads carries this body as it is.

        Install uses it to ask for ``apm compile`` only when a routed instruction
        is new or changed (its ``applyTo`` or body, even emptied), not on every
        no-op reinstall. The file must still hold the bytes compile recorded,
        wherever the shared optimizer placed the instruction, and its sibling
        ``CLAUDE.md`` must import it (a pre-#3939 compile does not).
        """
        return self.carrier(apply_to, body) is not None

    def carrier(self, apply_to: object, body: str) -> Path | None:
        """The compiled ``AGENTS.md`` Claude loads that carries this body, if any (#5343).

        Install writes no ``.claude/rules/`` file for a routed instruction only
        when one exists, and names it when it retires the rule.
        """
        files = self._fingerprint_index().get(instruction_fingerprint(apply_to, body), ())
        for agents_md in files:
            if self._current.get(agents_md) and self._claude_loads(agents_md.parent):
                return agents_md
        if not self._current:
            return self._destination_carrier(apply_to, body)
        return None

    def _destination_carrier(self, apply_to: object, body: str) -> Path | None:
        """With no record for this root (a fresh clone), read the destination (#5344).

        The record lives in APM home, so a committed compiled ``AGENTS.md`` is
        recognized when Claude loads it and it holds the body verbatim, as
        whole lines under the instruction's own scope section; a body compile
        rewrote (links) still asks for ``apm compile``.
        """
        text = body.replace("\r\n", "\n").strip()
        prefix = "" if _is_always_on(apply_to) else directory_scope(apply_to)
        if not text or prefix is None:
            return None
        directory = self.project_root / prefix if prefix else self.project_root
        if directory not in self._texts:
            self._texts[directory] = _read_text(directory / "AGENTS.md", self.project_root)
        compiled = self._texts[directory]
        carries = (
            compiled is not None
            and _section_carries_block(compiled, apply_to, text)
            and self._claude_loads(directory)
        )
        return directory / "AGENTS.md" if carries else None


# The section headings compile's instruction renderer writes (template_builder).
_SECTION_HEADING = re.compile(r"^## (?:Global Instructions|Files matching `.*`)$")


def _section_carries_block(compiled: str, apply_to: object, text: str) -> bool:
    """Whether *compiled* holds *text* as whole lines under its own scope section.

    The body's lines must appear as one contiguous run of complete lines
    inside the section compile writes for this ``applyTo`` (``## Global
    Instructions`` or ``## Files matching `<applyTo>```), bounded as a whole
    block, so "use tabs" never matches inside "never use tabs", a shortened
    body never matches a longer stale block, and no other scope counts (#5343).
    """
    scope = normalize_apply_to(apply_to, default="")
    heading = f"## Files matching `{scope}`" if scope else "## Global Instructions"
    lines = [line.rstrip() for line in compiled.replace("\r\n", "\n").split("\n")]
    block = [line.rstrip() for line in text.split("\n")]
    for start, line in enumerate(lines):
        if line != heading:
            continue
        end = next(
            (i for i in range(start + 1, len(lines)) if _SECTION_HEADING.match(lines[i])),
            len(lines),
        )
        section = lines[start + 1 : end]
        if any(
            section[i : i + len(block)] == block and _block_bounded(section, i, i + len(block))
            for i in range(len(section) - len(block) + 1)
        ):
            return True
    return False


def _block_bounded(section: list[str], start: int, end: int) -> bool:
    """Whether ``section[start:end]`` is a whole emitted block, not part of one.

    Compile separates blocks with a blank line (or a ``Source`` comment), so a
    shortened body never matches the head or tail of a longer stale one.
    """
    before = section[start - 1] if start > 0 else ""
    after = section[end] if end < len(section) else ""
    return (not before or before.startswith("<!-- Source:")) and (
        not after or after.startswith("<!-- End source:")
    )


def refresh_compiled_digest(base_dir: Path, agents_md: Path) -> None:
    """Re-pin a recorded ``AGENTS.md`` after a later step rewrote it (constitution)."""
    from ..core.agents_md_compile_record import read_record, write_record

    files = read_record(base_dir)
    entry = files.get(portable_relpath(agents_md, base_dir))
    digest = _file_digest(agents_md)
    if isinstance(entry, dict) and digest is not None:
        entry["sha256"] = digest
        write_record(base_dir, files)


def compiled_agents_md_may_carry(project_root: Path) -> bool:
    """Whether a compiled ``AGENTS.md`` may stand in for a Claude rule here (#5343).

    The lockfile records only the rules install wrote, not the ones it left
    to a carrying ``AGENTS.md``, so a fast path that replays the lock must
    defer to the full run when APM home records a compile for this root or a
    root ``AGENTS.md`` or ``CLAUDE.md`` exists.
    """
    from ..core.agents_md_compile_record import may_carry

    return may_carry(project_root)


def lock_ships_routable_instruction(project_root: Path, lock: object) -> bool:
    """Whether a locked package ships an instruction Claude may load from AGENTS.md (#5434).

    Routing reads carriers anywhere under the project, and a directory
    instruction routes once its directory and carrier exist, so this is the
    static form (:func:`routes_to_agents_md` without a root). A fast path that
    replays the lock declines for any such instruction; an unreadable one, or a
    missing package copy the engine would restore, counts.
    """
    from ..constants import APM_MODULES_DIR
    from ..integration.instruction_integrator import InstructionIntegrator

    modules = project_root / APM_MODULES_DIR
    integrator = InstructionIntegrator()
    for key, dependency in getattr(lock, "dependencies", {}).items():
        if key == ".":
            continue
        try:
            package = dependency.to_dependency_ref().get_install_path(modules)
            if package.is_symlink() or not package.is_dir():
                return True  # a copy the engine would restore: unknown content counts
            for source in integrator.find_instruction_files(package):
                metadata = integrator._parse_frontmatter(source.read_text(encoding="utf-8"))[0]
                if routes_to_agents_md(metadata.get("applyTo")):
                    return True
        except Exception:  # unreadable: the full run decides
            return True
    return False


def compiled_agents_md_has(project_root: Path, apply_to: object, body: str) -> bool:
    """One-off :meth:`CompiledCoverage.has` (install reuses one instance per root)."""
    return CompiledCoverage(project_root).has(apply_to, body)
