---
title: "Primitive Types"
sidebar:
  order: 3
---

This legacy note describes an earlier primitive discovery design. For current deployable primitive contracts, use [Package types](../package-types/) and [Targets matrix](../targets-matrix/).

## Overview

The enhanced primitive discovery system extends the existing primitive discovery functionality to support:

- **Dependency-aware discovery**: Scan primitives from both local `.apm/` directories and dependency packages in `apm_modules/`
- **Source tracking**: Every primitive knows where it came from (`local` or `dependency:{package_name}`)
- **Priority system**: Local primitives always override dependency primitives; dependencies processed in declaration order
- **Conflict detection**: Track when multiple sources provide the same primitive and report which source wins

## Key Features

### 1. Source Tracking

All primitive models (`Chatmode`, `Instruction`, `Context`) now include an optional `source` field:

```python
from apm_cli.primitives import Chatmode

# Local primitive
chatmode = Chatmode(
    name="assistant", 
    file_path=Path("local.agent.md"),
    description="Local assistant",
    content="...",
    source="local"  # New field
)

# Dependency primitive  
dep_chatmode = Chatmode(
    name="reviewer",
    file_path=Path("dep.agent.md"), 
    description="Dependency assistant",
    content="...",
    source="dependency:company-standards"  # New field
)
```

### 2. Enhanced Discovery Functions

#### `discover_primitives_with_dependencies(base_dir=".")`

Main enhanced discovery function that:
1. Scans local `.apm/` directory (highest priority)
2. Scans dependency packages in `apm_modules/` (lower priority, in declaration order)
3. Applies conflict resolution (local always wins)
4. Returns `PrimitiveCollection` with source tracking and conflict information

#### `discover_primitives(base_dir=".")`

Original discovery function - unchanged for backward compatibility. Only scans local primitives.

### 3. Conflict Detection

The `PrimitiveCollection` class now tracks conflicts when multiple sources provide primitives with the same name:

```python
collection = discover_primitives_with_dependencies()

# Check for conflicts
if collection.has_conflicts():
    for conflict in collection.conflicts:
        print(f"Conflict: {conflict}")
        # Output: "chatmode 'assistant': local overrides dependency:company-standards"
```

### 4. Priority System

1. **Local primitives always win**: Primitives in local `.apm/` directory override any dependency primitives with the same name
2. **Dependency order matters**: Dependencies are processed in the order declared in `apm.yml`; first declared dependency wins conflicts with later dependencies

### 5. Source-based Filtering

```python
collection = discover_primitives_with_dependencies()

# Get primitives by source
local_primitives = collection.get_primitives_by_source("local")
dep_primitives = collection.get_primitives_by_source("dependency:package-name")

# Get conflicts by type
chatmode_conflicts = collection.get_conflicts_by_type("chatmode")
```

## Usage Examples

### Basic Enhanced Discovery

```python
from apm_cli.primitives import discover_primitives_with_dependencies

# Discover all primitives (local + dependencies)
collection = discover_primitives_with_dependencies("/path/to/project")

print(f"Total primitives: {collection.count()}")
print(f"Conflicts detected: {collection.has_conflicts()}")

for primitive in collection.all_primitives():
    print(f"- {primitive.name} from {primitive.source}")
```

### Handling Conflicts

```python
collection = discover_primitives_with_dependencies()

if collection.has_conflicts():
    print("Conflicts detected:")
    for conflict in collection.conflicts:
        print(f"  - {conflict.primitive_name}: {conflict.winning_source} wins")
        print(f"    Overrides: {', '.join(conflict.losing_sources)}")
```

### Dependency Declaration Order

The system reads `apm.yml` to determine the order in which direct dependencies should be processed. Transitive dependencies (resolved automatically via dependency chains) are read from `apm.lock.yaml` and appended after direct dependencies:

```yaml
# apm.yml
name: my-project
version: 1.0.0
dependencies:
  apm:
    - company/standards#v1.0.0
    - team/workflows
    - user/utilities
```

Direct dependencies are processed first, in declaration order. Transitive dependencies from `apm.lock.yaml` are appended after. If multiple dependencies provide primitives with the same name, the first one declared wins.

## Directory Structure

The enhanced discovery system expects this structure:

```
project/
├── apm.yml                           # Dependency declarations
├── .apm/                             # Local primitives (highest priority)
│   ├── agents/
│   ├── instructions/
│   └── contexts/
└── apm_modules/                      # Dependency primitives
    ├── standards/                    # From company/standards
    │   └── .apm/
    │       ├── agents/
    │       └── instructions/
    ├── team/
    │   └── workflows/                # From team/workflows
    │       └── .apm/
    │           └── contexts/
    └── utilities/                    # From user/utilities
        └── .apm/
            └── instructions/
```

## Backward Compatibility

All changes are fully backward compatible:

- Existing `discover_primitives()` function unchanged
- Existing primitive constructors work unchanged (source field is optional)
- Existing `PrimitiveCollection` methods work unchanged
- All existing tests continue to pass

## Integration Points

The enhanced discovery system integrates with:

- **APM Package Models**: Uses `APMPackage` and `DependencyReference` from Task 1 to parse `apm.yml`
- **Existing Parser**: Extends existing primitive parser with optional source parameter
- **Future Compilation**: Prepared for integration with compilation system for source attribution

## Technical Details

### Conflict Resolution Algorithm

1. Create empty `PrimitiveCollection`
2. Scan local `.apm/` directory, add all primitives with `source="local"`
3. Parse `apm.yml` to get dependency declaration order
4. For each dependency in order:
   - Scan `apm_modules/{dependency}/.apm/` directory
   - Add primitives with `source="dependency:{dependency}"`
   - If primitive name conflicts with existing primitive:
     - Keep existing primitive (higher priority)
     - Record conflict with losing source information

Conflict detection uses O(1) name-indexed lookups, so performance remains constant regardless of collection size.

### Error Handling

- Gracefully handles missing `apm_modules/` directory
- Gracefully handles missing `apm.yml` file
- Gracefully handles invalid dependency directories
- Continues processing other dependencies if one fails
- Reports warnings for unparseable primitive files

## Testing

Comprehensive test suite in `tests/test_enhanced_discovery.py` covers:

- Source tracking functionality
- Conflict detection accuracy
- Priority system validation
- Dependency order parsing
- Backward compatibility
- Edge cases and error conditions

Run tests with:

```bash
python -m pytest tests/test_enhanced_discovery.py -v
```

## Future Enhancements

The enhanced discovery system is designed to support future features:

- **Compilation Integration**: Source attribution in generated `AGENTS.md` files
- **CLI Commands**: `apm deps list`, `apm compile --trace` commands
- **Advanced Conflict Resolution**: User-configurable conflict resolution strategies
- **Performance Optimization**: Caching and incremental discovery for large projects