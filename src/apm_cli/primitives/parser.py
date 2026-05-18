"""Parser for primitive definition files."""

from pathlib import Path

import frontmatter

from .models import Chatmode, Context, Instruction, Primitive, Skill


def parse_skill_file(file_path: str | Path, source: str | None = None) -> Skill:
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


def parse_primitive_file(file_path: str | Path, source: str | None = None) -> Primitive:
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


def _parse_chatmode(
    name: str,
    file_path: Path,
    metadata: dict,
    content: str,
    source: str | None = None,
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
    return Chatmode(
        name=name,
        file_path=file_path,
        description=metadata.get("description", ""),
        apply_to=metadata.get("applyTo"),  # Optional for chatmodes
        content=content,
        author=metadata.get("author"),
        version=metadata.get("version"),
        source=source,
    )


def _parse_instruction(
    name: str,
    file_path: Path,
    metadata: dict,
    content: str,
    source: str | None = None,
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
        apply_to=metadata.get("applyTo", ""),  # Required for instructions
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
    source: str | None = None,
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


def _extract_primitive_name(file_path: Path) -> str:
    """Extract primitive name from file path based on naming conventions.

    Args:
        file_path (Path): Path to the primitive file.

    Returns:
        str: Extracted primitive name.
    """
    path_parts = file_path.parts

    if ".apm" in path_parts or ".github" in path_parts:
        name = _extract_from_structured_directory(file_path, path_parts)
        if name:
            return name

    return _extract_from_filename(file_path)


def _extract_from_structured_directory(file_path: Path, path_parts: tuple) -> str | None:
    """Extract primitive name from structured .apm or .github directory."""
    try:
        base_idx = path_parts.index(".apm") if ".apm" in path_parts else path_parts.index(".github")

        if base_idx + 2 < len(path_parts) and path_parts[base_idx + 1] in [
            "chatmodes",
            "instructions",
            "context",
            "memory",
            "agents",
        ]:
            return _strip_extension(file_path.name)
    except (ValueError, IndexError):
        pass
    return None


def _strip_extension(basename: str) -> str:
    """Strip double extension from primitive filename."""
    extension_map = {
        ".chatmode.md": "",
        ".instructions.md": "",
        ".context.md": "",
        ".memory.md": "",
        ".agent.md": "",
        ".md": "",
    }
    for ext, replacement in extension_map.items():
        if basename.endswith(ext):
            return basename.replace(ext, replacement)
    return basename


def _extract_from_filename(file_path: Path) -> str:
    """Fallback: extract primitive name from filename."""
    name = _strip_extension(file_path.name)
    return name if name else file_path.stem


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
