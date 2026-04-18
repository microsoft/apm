---
applyTo: "src/apm_cli/cli.py"
description: "CLI Design Guidelines for visual output, styling, and user experience standards"
---

# CLI Design Guidelines

## Visual Design Standards

### Rich Library Usage
- **ALWAYS** use Rich library for visual output when available
- Provide graceful fallbacks to colorama for compatibility
- Use the established `console` instance with custom theme
- Wrap Rich imports in try/catch with colorama fallbacks

### Command Help Text
- Keep command help strings plain ASCII — no emojis
- Format: `help="Initialize a new APM project"`

### Status Symbols & Feedback
- Use `STATUS_SYMBOLS` dict for consistent ASCII bracket notation:
  - `[+]` success / confirmed
  - `[>]` running / execution / progress
  - `[*]` action / configuration / processing
  - `[i]` information / tips
  - `[#]` lists / metrics
  - `[!]` warnings
  - `[x]` errors
- Use helper functions: `_rich_success()`, `_rich_error()`, `_rich_info()`, `_rich_warning()`
- Pass the appropriate key from `STATUS_SYMBOLS` via the `symbol=` parameter (e.g. `symbol="check"`, `symbol="warning"`)

### Structured Output
- **Tables**: Use Rich tables for structured data (scripts, models, config, runtimes)
- **Panels**: Use Rich panels for grouped content, next steps, examples
- **Consistent Spacing**: Add empty lines between sections with `console.print()` or `click.echo()`

### Error Handling
- Use `_rich_error()` for all error messages
- Always include contextual symbols
- Provide actionable suggestions when possible
- Maintain consistent error message format

### Interactive Elements
- Use Rich `Prompt.ask()` and `Confirm.ask()` when available
- Provide click fallbacks for compatibility
- Display confirmations in Rich panels when possible

## Code Organization

### Helper Functions
- Use existing helper functions: `_rich_echo()`, `_rich_panel()`, `_create_files_table()`
- Create new helpers following the same pattern
- Always include Rich/colorama fallback logic

### Color Scheme
- Primary: cyan for titles and highlights
- Success: green with `[+]` symbol
- Warning: yellow with `[!]` symbol
- Error: red with `[x]` symbol
- Info: blue with `[i]` symbol
- Muted: dim white for secondary text

### Table Design
- Include meaningful titles (plain ASCII, no emojis)
- Use semantic column styling (bold for names, muted for details)
- Keep tables clean with appropriate padding
- Show status with bracket symbols in dedicated columns

## Implementation Patterns

### Command Structure
```python
@cli.command(help="Action description")
@click.option(...)
def command_name(...):
    """Detailed docstring."""
    try:
        _rich_info("Starting operation...", symbol="gear")
        
        # Main logic here
        
        _rich_success("Operation complete!", symbol="check")
    except Exception as e:
        _rich_error(f"Error: {e}", symbol="error")
        sys.exit(1)
```

### Table Creation
```python
try:
    table = Table(title="Title", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold white")
    table.add_column("Details", style="white")
    console.print(table)
except (ImportError, NameError):
    # Colorama fallback
```

### Panel Usage
```python
try:
    _rich_panel(content, title="Section Title", style="cyan")
except (ImportError, NameError):
    # Simple text fallback
```

## Quality Standards

### User Experience
- Every action should have clear visual feedback
- Group related information in panels or tables
- Use consistent symbols throughout the application
- Provide helpful next steps and examples

### Accessibility
- Maintain colorama fallbacks for all Rich features
- Use semantic text alongside visual elements
- Ensure information is conveyed through text, not just color

### Performance
- Import Rich modules only when needed
- Handle import failures gracefully
- Don't block on visual enhancements

## Examples to Follow

- **init command**: Shows Rich panels, file tables, next steps
- **list command**: Professional table with default script indicators  
- **preview command**: Side-by-side panels for original/compiled
- **config command**: Clean configuration display

## What NOT to Do

- **Never** use plain `click.echo()` without styling
- **Never** mix color schemes or symbols inconsistently
- **Never** create walls of text without visual structure
- **Never** forget Rich import fallbacks
- **Never** sacrifice functionality for visuals
- **Never** use emojis or non-ASCII characters in source code or CLI output

## Documentation Sync Requirements

### CLI Reference Documentation
- **ALWAYS** update `docs/src/content/docs/reference/cli-commands.md` when adding, modifying, or removing CLI commands
- **ALWAYS** update command help text, options, arguments, and examples in the reference
- **ALWAYS** verify examples in the documentation actually work with the current implementation
- **ALWAYS** keep the command list in sync with available commands

### Documentation Update Checklist
When changing CLI functionality, update these sections in `docs/src/content/docs/reference/cli-commands.md`:
- Command syntax and arguments
- Available options and flags  
- Usage examples
- Return codes and error handling
- Quick reference sections

### Documentation Standards
- Use plain ASCII text in documentation (no emojis in CLI help text or output examples)
- Include realistic, working examples that users can copy-paste
- Document both success and error scenarios
- Keep examples current with the latest syntax
- Maintain consistency between CLI help and reference documentation
