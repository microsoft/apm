---
applyTo: "**/*.py"
description: "Python development guidelines"
---

## Python Development Standards

### Code Quality
- Follow PEP 8 style guidelines
- Use type hints for all function signatures
- Implement proper docstrings for modules, classes, and functions
- Apply the principle of least astonishment

### Project Structure
- Use virtual environments for dependency isolation
- Organize code into logical packages and modules
- Implement proper __init__.py files for packages
- Follow standard project layout conventions

### Testing and Quality
- Write comprehensive unit tests with pytest
- Use meaningful test names that describe behavior
- Implement integration tests for external dependencies
- Maintain test coverage above 85%
- Use ruff for linting and formatting, mypy for type checking

See [project architecture](../context/architecture.context.md) for detailed patterns.