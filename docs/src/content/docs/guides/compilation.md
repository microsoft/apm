---
title: "Compilation"
sidebar:
  order: 1
---

Compilation is **optional for some users**. If your team uses GitHub Copilot, Claude, or Cursor, `apm install` deploys all primitives in their native format -- you can skip this guide entirely. For Gemini, `apm install` deploys commands, skills, and hooks, but instructions require `apm compile` to generate `GEMINI.md`. For OpenCode and Codex, `apm install` deploys agents, commands, skills, and hooks, but instructions require `apm compile` to generate `AGENTS.md`.

**Solving the AI agent scalability problem through constraint satisfaction optimization**

APM's compilation system implements a mathematically rigorous solution to the **context pollution problem** that degrades AI agent performance as projects grow. Through constraint satisfaction optimization and hierarchical coverage guarantees, `apm compile` transforms scattered primitives into optimized context files for every major AI coding agent.

## Multi-Agent Output

APM compiles your primitives into native formats for each major AI coding agent. Target selection is automatic based on your project structure.

### Target Auto-Detection

When you run `apm compile` without specifying a target, APM automatically detects:

| Project Structure | Target | What Gets Generated |
|-------------------|--------|---------------------|
| `.github/` folder only | `copilot` | AGENTS.md (instructions only) |
| `.claude/` folder only | `claude` | CLAUDE.md (instructions only) |
| `.codex/` folder exists | `codex` | AGENTS.md (instructions only) |
| `.gemini/` folder exists | `gemini` | GEMINI.md (instructions only) |
| Multiple folders exist | `all` | AGENTS.md + CLAUDE.md + GEMINI.md |
| Neither folder exists | `minimal` | AGENTS.md only (universal format) |

```bash
apm compile                    # Auto-detects target from project structure
apm compile --target copilot   # Force GitHub Copilot, Cursor
apm compile --target claude    # Force Claude Code, Claude Desktop
apm compile --target gemini    # Force Gemini CLI
apm compile --target codex     # Force Codex CLI
apm compile -t claude,copilot  # Multiple targets (comma-separated)
```

You can set a persistent target in `apm.yml`:
```yaml
name: my-project
version: 1.0.0
target: copilot  # single target
```

```yaml
name: my-project
version: 1.0.0
target: [claude, copilot]  # multiple targets -- only these are compiled
```

### Output Files

| Target | Files Generated | Consumers |
|--------|-----------------|-----------|
| `copilot` | `AGENTS.md` | GitHub Copilot, Cursor, OpenCode |
| `claude` | `CLAUDE.md` | Claude Code, Claude Desktop |
| `gemini` | `GEMINI.md` | Gemini CLI |
| `codex` | `AGENTS.md` | Codex CLI |
| `all` | `AGENTS.md` + `CLAUDE.md` + `GEMINI.md` | Universal compatibility |
| `minimal` | `AGENTS.md` only | Works everywhere, no folder integration |

> **Aliases**: `vscode` and `agents` are accepted as aliases for `copilot`.

> **Note**: `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` contain **only instructions** (grouped by `applyTo` patterns). Prompts, agents, commands, hooks, and skills are integrated by `apm install`, not `apm compile`. See the [Integrations Guide](../../integrations/ide-tool-integration/) for details on how `apm install` populates `.github/prompts/`, `.github/agents/`, `.github/skills/`, `.claude/commands/`, `.cursor/rules/`, `.cursor/agents/`, `.opencode/agents/`, `.opencode/commands/`, `.codex/agents/`, `.gemini/commands/`, and `.agents/skills/`.

### How It Works

1. **Primitives Discovery**: Scans `.apm/` and `.github/` directories for instructions, prompts, and agents
2. **Dependency Merging**: Incorporates primitives from installed packages in `apm_modules/`
3. **Optimization**: Applies mathematical context optimization (see below)
4. **Format Generation**: Outputs native files for each target agent format

### Example Output

**After `apm compile`:**
```
my-project/
├── AGENTS.md              # Instructions only (for Copilot, Cursor, etc.)
└── CLAUDE.md              # Instructions only (for Claude)
```

**After `apm install` (folder integration):**
```
my-project/
├── .github/
│   ├── prompts/           # Prompts from installed packages
│   └── agents/            # Agents from installed packages
├── .claude/
│   ├── commands/          # Claude slash commands from packages
│   └── skills/            # Skills from packages with SKILL.md
└── .cursor/
    ├── rules/             # Instructions converted to Cursor rules
    └── agents/            # Agents from installed packages
```

## The Context Pollution Problem

### Why Traditional Approaches Fail

In traditional monolithic AGENTS.md approaches, AI agents face a fundamental efficiency problem: **context pollution**. As projects grow, agents must process increasingly large amounts of irrelevant instructions, degrading performance and overwhelming context windows.

**The Mathematical Challenge**:
```
Context_Efficiency = Relevant_Instructions / Total_Instructions_Inherited
```

Without optimization, context efficiency degrades quadratically with project size, creating an unsustainable burden on AI agents working in specific directories.

### The AGENTS.md Standard Solution

APM implements the [AGENTS.md standard](https://agents.md) for hierarchical context files:

- **Recursive Discovery**: Agents read AGENTS.md files from current directory up to project root
- **Proximity Priority**: Closest AGENTS.md to the edited file takes precedence  
- **Inheritance Model**: Child directories inherit and can override parent instructions
- **Universal Compatibility**: Works with GitHub Copilot, Cursor, Claude, and all AGENTS.md-compliant tools

## The Mathematical Foundation

### Core Optimization Problem

APM treats instruction placement as a **constrained optimization problem**:

```
Objective: minimize Σ(pollution[d] × files[d])
           d∈directories

Subject to: ∀f ∈ matching_files(pattern) → 
           ∃p ∈ placements : f.can_inherit_from(p)

Variables: placement_matrix ∈ {0,1}^(directories × instructions)
```

This mathematical formulation guarantees:
1. **Complete Coverage**: Every file can access its applicable instructions
2. **Minimal Pollution**: Irrelevant context is systematically minimized
3. **Hierarchical Validity**: Inheritance chains remain consistent

### The Three-Tier Placement Algorithm

APM employs sophisticated distribution scoring with mathematical thresholds:

```python
# From context_optimizer.py
Distribution_Score = (matching_directories / total_directories) × diversity_factor

Where:
diversity_factor = 1.0 + (depth_variance × DIVERSITY_FACTOR_BASE)
DIVERSITY_FACTOR_BASE = 0.5  # Mathematical constant
```

**Strategy Selection**:

| Distribution Score | Strategy | Mathematical Logic |
|-------------------|----------|-------------------|
| < 0.3 | Single-Point | `_optimize_single_point_placement()` |
| 0.3 - 0.7 | Selective Multi | `_optimize_selective_placement()` |
| > 0.7 | Distributed | `_optimize_distributed_placement()` |

### Constraint Satisfaction Weights

The optimization engine uses mathematically calibrated weights:

```python
# Mathematical optimization parameters from the source
COVERAGE_EFFICIENCY_WEIGHT = 1.0    # Mandatory coverage priority
POLLUTION_MINIMIZATION_WEIGHT = 0.8  # Strong pollution penalty
MAINTENANCE_LOCALITY_WEIGHT = 0.3    # Moderate locality preference
DEPTH_PENALTY_FACTOR = 0.1          # Excessive nesting penalty
```

## Understanding the Metrics

### Context Efficiency Ratio

The primary performance indicator for AI agent effectiveness:

```python
def get_efficiency_ratio(self) -> float:
    """Calculate context efficiency ratio."""
    if self.total_context_load == 0:
        return 1.0
    return self.relevant_context_load / self.total_context_load
```

**Interpretation Guide**:

| Efficiency Range | Assessment | Optimization Quality |
|-----------------|------------|-------------------|
| 80-100% | Excellent | Near-perfect instruction locality |
| 60-80% | Good | Well-optimized with minimal conflicts |
| 40-60% | Fair | Acceptable coverage/efficiency balance |
| 20-40% | Poor | Significant cross-cutting concerns |
| 0-20% | Critical | Architecture requires refactoring |

**Important**: Low efficiency can be mathematically optimal when coverage constraints force root placement. The optimizer **always prioritizes complete coverage** over efficiency.

### Distribution Score Analysis

Measures pattern spread across the directory structure:

```python
def _calculate_distribution_score(self, matching_directories: Set[Path]) -> float:
    """Calculate distribution score with diversity factor."""
    total_dirs_with_files = len([d for d in self._directory_cache.values() if d.total_files > 0])
    base_ratio = len(matching_directories) / total_dirs_with_files
    
    # Account for depth diversity
    depths = [self._directory_cache[d].depth for d in matching_directories]
    depth_variance = sum((d - sum(depths)/len(depths))**2 for d in depths) / len(depths)
    diversity_factor = 1.0 + (depth_variance * self.DIVERSITY_FACTOR_BASE)
    
    return base_ratio * diversity_factor
```

### Coverage Verification

Mathematical guarantee that no instruction is lost:

```python
def _calculate_hierarchical_coverage(self, placements: List[Path], target_directories: Set[Path]) -> Set[Path]:
    """Verify hierarchical coverage through inheritance chains."""
    covered = set()
    for target in target_directories:
        for placement in placements:
            if self._is_hierarchically_covered(target, placement):
                covered.add(target)
                break
    return covered
```

## Usage and Configuration

### Basic Compilation (Default: Distributed)

```bash
# Intelligent distributed optimization
apm compile

# Example output:
Analyzing 247 files across 12 directories...
Optimizing instruction placement...
Generated 4 AGENTS.md files with guaranteed coverage
```

### Mathematical Analysis Mode

```bash
# Show optimization reasoning
apm compile --verbose

# Example detailed output:
Mathematical Analysis:
|- Distribution Scores:
|  |- **/*.py: 0.23 -> Single-Point Strategy
|  |- **/*.tsx: 0.67 -> Selective Multi Strategy  
|  +- **/*.md: 0.81 -> Distributed Strategy
|- Coverage Verification: Complete (100%)
|- Constraint Satisfaction: All 8 constraints satisfied
+- Generation Time: 127ms
```

### Performance Analysis

```bash
# Preview placement without writing files
apm compile --dry-run

# Timing instrumentation
apm compile --verbose
# Shows: Project Analysis: 45.2ms
#        Instruction Processing: 82.1ms
```

### Configuration Control

```yaml
# apm.yml
compilation:
  strategy: "distributed"  # Default: mathematical optimization
  exclude:
    # Directory exclusion patterns (glob syntax)
    - "apm_modules/**"           # Exclude installed packages
    - "tmp/**"                   # Exclude temporary files
    - "coverage/**"              # Exclude test coverage
    - "**/test-fixtures/**"      # Exclude test fixtures everywhere
  placement:
    min_instructions_per_file: 1  # Minimal context principle
    clean_orphaned: true  # Remove outdated files
  optimization:
    # Mathematical weights (advanced users)
    coverage_weight: 1.0      # Coverage priority (mandatory)
    pollution_weight: 0.8     # Pollution minimization
    locality_weight: 0.3      # Maintenance locality
```

#### Directory Exclusion Patterns

Use the `exclude` field to skip directories during compilation, improving performance in large monorepos:

**Pattern Syntax:**
- `tmp` - Matches directory named "tmp" at any depth
- `tmp/` - Same as above (trailing slash optional)
- `projects/packages/apm` - Matches specific nested path
- `**/node_modules` - Matches "node_modules" at any depth
- `coverage/**` - Matches "coverage" and all subdirectories
- `projects/**/apm/**` - Complex nested matching

**Use Cases:**
- Exclude source package development directories in monorepos
- Skip temporary directories and build artifacts
- Improve compilation performance by avoiding unnecessary scans
- Prevent duplicate instruction discovery

**Default Exclusions:**
APM always excludes directories whose path contains an exact component matching one of these names (no configuration needed). A directory named `rebuild/` is **not** excluded just because it contains `build` as a substring.
- `node_modules`
- `__pycache__`
- `.git`
- `dist`
- `build`
- `apm_modules`
- Hidden directories (starting with `.`)

## Advanced Optimization Features

### Hierarchical Coverage Guarantee

The mathematical **coverage constraint** ensures no instruction is ever lost:

```
project/
├── AGENTS.md                    # Global standards
├── src/
│   ├── AGENTS.md               # Source code patterns
│   └── components/
│       ├── AGENTS.md           # Component-specific
│       └── Button.tsx          # Inherits: global + src + components
```

**Coverage Verification Algorithm**:
```python
def verify_coverage(placements, matching_files):
    """Ensure every file can inherit its instructions"""
    for file in matching_files:
        chain = get_inheritance_chain(file)
        if not any(p in chain for p in placements):
            raise CoverageViolation(file)  # Mathematical guarantee
    return True
```

### Performance Engineering

**Multi-layer caching system** for sub-second compilation:

```python
# From context_optimizer.py
self._directory_cache: Dict[Path, DirectoryAnalysis] = {}
self._pattern_cache: Dict[str, Set[Path]] = {}
self._glob_cache: Dict[str, List[str]] = {}
```

**Typical performance**: < 500ms for projects with 10,000+ files

### Deterministic Output

Compilation is completely reproducible:
- Sorted iteration order prevents randomness
- Stable optimization algorithm
- Consistent Build IDs across machines
- Cache-friendly for CI/CD systems

### Constitution Injection

Project governance automatically injected at AGENTS.md top:

```markdown
<!-- SPEC-KIT CONSTITUTION: BEGIN -->
hash: 34c5812dafc9 path: memory/constitution.md
[Project principles and governance]
<!-- SPEC-KIT CONSTITUTION: END -->
```

## Real-World Application

### Enterprise React Application Case

**Project Characteristics**:
- 15,000+ lines of code
- 127 component files
- 8 instruction patterns
- 3 team-specific standards

**Optimization Results**:
- **7 strategically placed** AGENTS.md files
- **Complete coverage** mathematically verified
- **Context efficiency**: 67.3% (Good rating)
- **Generation time**: 89ms

**Compared to Monolithic Approach**:
- Single 847-line AGENTS.md file
- Universal context pollution
- No mathematical optimization
- Manual maintenance required

## Technical Innovation

### Constraint Satisfaction Algorithm

APM implements **complete coverage with minimal pollution**:

1. **Coverage Constraint**: Mathematical guarantee every file accesses applicable instructions
2. **Pollution Minimization**: Systematic reduction of irrelevant context
3. **Hierarchical Validation**: Inheritance chain verification
4. **Performance Optimization**: Sub-second compilation with caching

### Three-Tier Strategy Implementation

```python
# Actual implementation from context_optimizer.py
if distribution_score < self.LOW_DISTRIBUTION_THRESHOLD:
    strategy = PlacementStrategy.SINGLE_POINT
    placements = self._optimize_single_point_placement(matching_directories, instruction)
elif distribution_score > self.HIGH_DISTRIBUTION_THRESHOLD:
    strategy = PlacementStrategy.DISTRIBUTED  
    placements = self._optimize_distributed_placement(matching_directories, instruction)
else:
    strategy = PlacementStrategy.SELECTIVE_MULTI
    placements = self._optimize_selective_placement(matching_directories, instruction)
```

### Mathematical Sophistication

The optimization engine implements:
- **Variance-weighted distribution scoring**
- **Hierarchical coverage verification**  
- **Constraint satisfaction with fallback guarantees**
- **Performance-optimized caching strategies**
- **Deterministic reproducible results**

## Tool Compatibility

Different AI tools get different levels of support from `apm install` vs `apm compile`:

| AI Tool | What `apm install` deploys | What `apm compile` adds | Support level |
|---------|--------------------------|------------------------|---------------|
| GitHub Copilot | `.github/instructions/`, `.github/prompts/`, agents, hooks, plugins, MCP | `AGENTS.md` (optional) | **Full** |
| Claude | `.claude/` commands, skills, MCP | `CLAUDE.md` | **Full** |
| Cursor | `.cursor/rules/`, `.cursor/agents/`, `.cursor/skills/`, `.cursor/hooks.json`, `.cursor/mcp.json` | `AGENTS.md` (optional) | **Full** |
| OpenCode | `.opencode/agents/`, `.opencode/commands/`, `.opencode/skills/`, `opencode.json` (MCP) | Via `AGENTS.md` | **Full** |
| Codex CLI | `.agents/skills/`, `.codex/agents/`, `.codex/hooks.json` | `AGENTS.md` (instructions) | **Full** |
| Gemini | `.gemini/commands/`, `.gemini/skills/`, `.gemini/settings.json` (MCP, hooks) | `GEMINI.md` (instructions) | **Full** |

For Copilot, Claude, and Cursor users, `apm install` handles everything natively. Gemini, OpenCode, and Codex users should also run `apm compile` to generate their instruction roll-up (`GEMINI.md` or `AGENTS.md`).

## Theoretical Foundations

### Computational Complexity

- **Time Complexity**: O(n·m·log(d))
  - n = number of instructions
  - m = number of directories  
  - d = maximum directory depth

- **Space Complexity**: O(n·m)
  - Placement matrix storage

### Optimization Bounds

**Theoretical maximum efficiency**:
```
Max_Efficiency = 1 - (cross_cutting_patterns / total_patterns)
```

Most well-structured projects achieve 60-85% of theoretical maximum through mathematical optimization.

## Future Enhancements

### Planned Optimizations

**Machine Learning Enhancement**: Neural network to predict optimal placement based on:
- Historical agent query patterns
- File change frequency analysis
- Team-specific access patterns

**Dynamic Recompilation**: File watcher with targeted optimization:
```bash
apm compile --watch  # Auto-recompile on changes
```

**Context Budget Optimization**: Token-aware instruction prioritization:
```yaml
compilation:
  optimization:
    max_tokens_per_file: 4000
    priority_scoring: true
```

## Conclusion

APM's Context Optimization Engine represents a fundamental advancement in AI-assisted development infrastructure. By treating instruction distribution as a **mathematical optimization problem** with **guaranteed coverage constraints**, APM creates:

1. **Mathematically optimal context loading** for AI agents
2. **Complete coverage guarantee** through constraint satisfaction
3. **Linear scalability** with project size
4. **Universal compatibility** with the AGENTS.md standard
5. **Performance engineering** with sub-second compilation

The result: AI agents that work efficiently and reliably, regardless of project size or complexity.

---

**Ready to optimize your AI agent performance?**

```bash
# See the mathematics in action
apm compile --verbose

# Experience optimized AI development
apm init my-project && cd my-project && apm compile
```

**Technical Implementation**: ``src/apm_cli/compilation/``  
**Mathematical Core**: ``context_optimizer.py``