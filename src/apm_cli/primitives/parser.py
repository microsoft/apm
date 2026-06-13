"""Parser for primitive definition files."""

from pathlib import Path

import frontmatter

from .models import Chatmode, Context, Instruction, Primitive, Skill


def parse_skill_file(file_path: str | Path, source: str = None) -> Skill:  # noqa: RUF013
    """Parse a SKILL.md file.

    SKILL.md files are package meta-guides that describe how to use the package.
    They have a simple frontmatter with 'name' and 'description' fields.

    Args:
        file_path (Union[str, Path]): Path to the SKILL.md file.
        source (str, optional): Source identifier (e.g., "local", "dependency:package_name").

    Returns:
        Skill: Parsed skill primitive.

    Raises:
        ValueError: If file cannot be parsed or has invalid format.
    """
    file_path = Path(file_path)

    try:
        with open(file_path, encoding="utf-8") as f:
            post = frontmatter.load(f)

        metadata = post.metadata
        content = post.content

        # Extract required fields from frontmatter
        name = metadata.get("name", "")
        description = metadata.get("description", "")

        # If name is missing, derive from parent directory name
        if not name:
            name = file_path.parent.name

        return Skill(
            name=name, file_path=file_path, description=description, content=content, source=source
        )

    except Exception as e:
        raise ValueError(f"Failed to parse SKILL.md file {file_path}: {e}")  # noqa: B904


def parse_primitive_file(file_path: str | Path, source: str = None) -> Primitive:  # noqa: RUF013
    """Parse a primitive file.

    Determines the primitive type based on file extension and parses accordingly.

    Args:
        file_path (Union[str, Path]): Path to the primitive file.
        source (str, optional): Source identifier for the primitive (e.g., "local", "dependency:package_name").

    Returns:
        Primitive: Parsed primitive (Chatmode, Instruction, or Context).

    Raises:
        ValueError: If file cannot be parsed or has invalid format.
    """
    file_path = Path(file_path)

    try:
        with open(file_path, encoding="utf-8") as f:
            post = frontmatter.load(f)

        # Extract name based on file structure
        name = _extract_primitive_name(file_path)
        metadata = post.metadata
        content = post.content

        # Determine primitive type based on file extension
        if file_path.name.endswith(".chatmode.md") or file_path.name.endswith(".agent.md"):
            return _parse_chatmode(name, file_path, metadata, content, source)
        elif file_path.name.endswith(".instructions.md"):
            return _parse_instruction(name, file_path, metadata, content, source)
        elif (
            file_path.name.endswith(".context.md")
            or file_path.name.endswith(".memory.md")
            or _is_context_file(file_path)
        ):
            return _parse_context(name, file_path, metadata, content, source)
        else:
            raise ValueError(f"Unknown primitive file type: {file_path}")

    except Exception as e:
        raise ValueError(f"Failed to parse primitive file {file_path}: {e}")  # noqa: B904


def _normalize_apply_to(value: object, default: str = "") -> str:
    """Normalize an applyTo frontmatter value to a string.

    YAML allows list-valued applyTo (e.g. ``applyTo: ['**/*.py']``).
    The rest of the compilation pipeline expects a plain string and treats
    apply_to as a single glob pattern -- it has no mechanism to split a
    comma-joined multi-pattern string back into individual globs.

    When a list is encountered, only the first non-null element is used.
    Multi-pattern support (``list[str]`` migration across all consumers)
    is tracked separately.

    Args:
        value: The raw value returned by the YAML parser (str, list, or None).
        default: Fallback when value is None or an empty list.

    Returns:
        str: Normalized glob pattern string.
    """
    if isinstance(value, list):
        non_null = [str(v) for v in value if v is not None]
        return non_null[0] if non_null else default
    if value is None:
        return default
    return str(value)


def _parse_chatmode(
    name: str,
    file_path: Path,
    metadata: dict,
    content: str,
    source: str = None,  # noqa: RUF013
) -> Chatmode:
    """Parse a chatmode primitive.

    Args:
        name (str): Name of the chatmode.
        file_path (Path): Path to the file.
        metadata (dict): Metadata from frontmatter.
        content (str): Content of the file.
        source (str, optional): Source identifier for the primitive.

    Returns:
        Chatmode: Parsed chatmode primitive.
    """
    raw_apply_to = metadata.get("applyTo")
    normalized_apply_to = _normalize_apply_to(raw_apply_to, default="") or None
    raw_handoffs = metadata.get("handoffs")
    handoffs: list[str | dict] | None = None
    if isinstance(raw_handoffs, list):
        # Preserve structured entries (dicts) -- str() coercion destroys VS Code handoff objects.
        handoffs = [h for h in raw_handoffs if h is not None]
    elif raw_handoffs is not None:
        handoffs = [str(raw_handoffs)]
    return Chatmode(
        name=name,
        file_path=file_path,
        description=metadata.get("description", ""),
        apply_to=normalized_apply_to,
        content=content,
        author=metadata.get("author"),
        version=metadata.get("version"),
        source=source,
        handoffs=handoffs,
    )


def _parse_instruction(
    name: str,
    file_path: Path,
    metadata: dict,
    content: str,
    source: str = None,  # noqa: RUF013
) -> Instruction:
    """Parse an instruction primitive.

    Args:
        name (str): Name of the instruction.
        file_path (Path): Path to the file.
        metadata (dict): Metadata from frontmatter.
        content (str): Content of the file.
        source (str, optional): Source identifier for the primitive.

    Returns:
        Instruction: Parsed instruction primitive.
    """
    return Instruction(
        name=name,
        file_path=file_path,
        description=metadata.get("description", ""),
        apply_to=_normalize_apply_to(metadata.get("applyTo"), default=""),
        content=content,
        author=metadata.get("author"),
        version=metadata.get("version"),
        source=source,
    )


def _parse_context(
    name: str,
    file_path: Path,
    metadata: dict,
    content: str,
    source: str = None,  # noqa: RUF013
) -> Context:
    """Parse a context primitive.

    Args:
        name (str): Name of the context.
        file_path (Path): Path to the file.
        metadata (dict): Metadata from frontmatter.
        content (str): Content of the file.
        source (str, optional): Source identifier for the primitive.

    Returns:
        Context: Parsed context primitive.
    """
    return Context(
        name=name,
        file_path=file_path,
        content=content,
        description=metadata.get("description"),  # Optional for contexts
        author=metadata.get("author"),
        version=metadata.get("version"),
        source=source,
    )


_PRIMITIVE_SUFFIXES = (
    ".chatmode.md",
    ".instructions.md",
    ".context.md",
    ".memory.md",
    ".agent.md",
    ".md",
)

_STRUCTURED_SUBDIRS = frozenset({"chatmodes", "instructions", "context", "memory", "agents"})


def _strip_file_ext(basename: str) -> str:
    """Strip the primitive double-extension from a basename, returning the stem.

    Tries each known suffix in priority order; returns *basename* unchanged when
    no suffix matches (so callers can detect "no strip happened").
    """
    for suffix in _PRIMITIVE_SUFFIXES:
        if basename.endswith(suffix):
            return basename[: -len(suffix)]
    return basename


def _extract_primitive_name(file_path: Path) -> str:
    """Extract primitive name from file path based on naming conventions.

    Args:
        file_path (Path): Path to the primitive file.

    Returns:
        str: Extracted primitive name.
    """
    path_parts = file_path.parts

    # Structured directory (.apm/ or .github/): strip double extension directly.
    if ".apm" in path_parts or ".github" in path_parts:
        try:
            base_idx = (
                path_parts.index(".apm") if ".apm" in path_parts else path_parts.index(".github")
            )
            if base_idx + 2 < len(path_parts) and path_parts[base_idx + 1] in _STRUCTURED_SUBDIRS:
                return _strip_file_ext(file_path.name)
        except (ValueError, IndexError):
            pass

    # Fallback: strip extension if recognised; otherwise use pathlib stem.
    stripped = _strip_file_ext(file_path.name)
    if stripped != file_path.name:
        return stripped
    return file_path.stem


def _is_context_file(file_path: Path) -> bool:
    """Check if a file should be treated as a context file based on its directory.

    Args:
        file_path (Path): Path to check.

    Returns:
        bool: True if file is in .apm/memory/ or .github/memory/ directory.
    """
    # Only files directly under .apm/memory/ or .github/memory/ are considered context files here
    parent_parts = file_path.parent.parts[-2:]  # Get last two parts of parent path
    return parent_parts in [(".apm", "memory"), (".github", "memory")]


def validate_primitive(primitive: Primitive) -> list[str]:
    """Validate a primitive and return any errors.

    Args:
        primitive (Primitive): Primitive to validate.

    Returns:
        List[str]: List of validation errors.
    """
    return primitive.validate()
