"""Prompt-to-command transform functions.

Pure functions for transforming ``.prompt.md`` source files into the
various command file formats supported by APM targets:

- Claude / shared ``claude_command`` format (YAML frontmatter + Markdown body).
- Gemini CLI ``.toml`` format.

These functions are intentionally free of ``CommandIntegrator`` state so
they can be tested in isolation and reused by other parts of the pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter

from ._input_helpers import (
    _PRESERVED_COMMAND_KEYS,
    _extract_input_names,
)


def _build_claude_metadata(post: frontmatter.Post) -> dict[str, object]:
    """Build preserved Claude command metadata from source frontmatter."""
    metadata: dict[str, object] = {}
    for target_key, source_keys in (
        ("description", ("description",)),
        ("allowed-tools", ("allowed-tools", "allowedTools")),
        ("model", ("model",)),
        ("argument-hint", ("argument-hint", "argumentHint")),
    ):
        for source_key in source_keys:
            if source_key in post.metadata:
                metadata[target_key] = post.metadata[source_key]
                break
    return metadata


def _transform_prompt_to_command(
    source: Path,
) -> tuple[str, frontmatter.Post, list[str], list[str]]:
    """Transform a ``.prompt.md`` file into Claude command format.

    Args:
        source: Path to the ``.prompt.md`` file.

    Returns:
        Tuple of (command_name, post, warnings, dropped_keys).
        ``dropped_keys`` lists source frontmatter keys that the shared
        command transformer does not preserve (e.g. ``author``,
        ``mcp``, ``parameters`` for Cursor-specific frontmatter).
    """
    warnings: list[str] = []

    post = frontmatter.load(source)

    # Extract command name from filename.
    filename = source.name
    if filename.endswith(".prompt.md"):
        command_name = filename[: -len(".prompt.md")]
    else:
        command_name = source.stem

    # Build Claude command frontmatter (preserve existing, add Claude-specific).
    claude_metadata = _build_claude_metadata(post)

    # Map APM 'input' to Claude 'arguments' and 'argument-hint'.
    input_names, rejected_names = _extract_input_names(post.metadata.get("input"))
    if rejected_names:
        warnings.append(
            f"input: rejected {len(rejected_names)} invalid name(s) "
            f"(must match [A-Za-z][\\w-]{{0,63}}): "
            f"{', '.join(rejected_names[:5])}" + (" ..." if len(rejected_names) > 5 else "")
        )
    if input_names:
        claude_metadata["arguments"] = input_names
        if "argument-hint" not in claude_metadata:
            claude_metadata["argument-hint"] = " ".join(f"<{name}>" for name in input_names)

    # Convert APM input references to Claude $name placeholders.
    content = post.content
    if input_names:
        content = re.sub(
            r"\$\{\{?\s*input\s*:\s*([\w-]+)\s*\}?\}",
            r"$\1",
            content,
        )

    # Create new post with Claude metadata.
    new_post = frontmatter.Post(content)
    new_post.metadata = claude_metadata

    # Compute keys present in source frontmatter but not preserved by
    # the shared command transformer.  Surfaced by integrate_command()
    # via diagnostics so users see the lossy transform at install time.
    dropped_keys = sorted(set(post.metadata.keys()) - _PRESERVED_COMMAND_KEYS)

    return (command_name, new_post, warnings, dropped_keys)


def _write_gemini_command(source: Path, target: Path) -> None:
    """Transform a ``.prompt.md`` file to Gemini CLI ``.toml`` format.

    Parses YAML frontmatter for ``description``, uses the markdown body as
    the ``prompt`` field.  Replaces ``$ARGUMENTS`` with ``{{args}}`` (Gemini
    CLI's argument interpolation syntax).

    Ref: https://geminicli.com/docs/cli/gemini-md/
    """
    import toml as _toml

    post = frontmatter.load(source)

    description = post.metadata.get("description", "")
    prompt_text = post.content.strip()
    prompt_text = prompt_text.replace("$ARGUMENTS", "{{args}}")

    if re.search(r"(?<!\d)\$\d+", prompt_text):
        prompt_text = f"Arguments: {{{{args}}}}\n\n{prompt_text}"

    doc: dict = {"prompt": prompt_text}
    if description:
        doc = {"description": description, "prompt": prompt_text}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_toml.dumps(doc), encoding="utf-8")
