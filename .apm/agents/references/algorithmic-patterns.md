# Algorithmic Performance Patterns

Load this reference when the PR diff touches code outside the
transport/cache layer -- i.e. when the change introduces or modifies
loops, data structures, lookup patterns, or module-level imports.

## Big O Quick Reference

| Pattern | Complexity | Red Flag |
|---------|-----------|----------|
| Dict/set lookup | O(1) | Fine |
| List `.append` | O(1) amortised | Fine |
| `x in list` | O(n) | Use a set if called in a loop |
| Nested loops over same collection | O(n^2) | Extract an index dict first |
| Sort inside a loop | O(n^2 log n) | Sort once outside the loop |
| `any(pred(x) for x in coll)` in a loop | O(n*m) | Build a set/dict pre-loop |
| Unconditional full-dir scan on every write | O(n) per write = O(n^2) total | Track running total; scan only when needed |
| Linear search for identity match | O(n) per lookup | Build `{identity: index}` once |

## Anti-Patterns to Flag

### 1. Missing Index on Repeated Lookup

```python
# BAD: O(n) per call, called m times = O(n*m)
def has_item(collection, key):
    return any(item.key == key for item in collection)

# GOOD: O(1) per call after O(n) setup
_index = {item.key for item in collection}
def has_item(key):
    return key in _index
```

Flag when: a function does linear scan AND is called from within a
loop or from a method called repeatedly during resolution/install.

### 2. Unconditional Expensive Operation

```python
# BAD: scans entire cache dir on every store()
def store(self, url, body):
    self._write(url, body)
    self._enforce_size_cap()  # full scandir every time

# GOOD: fast-path skip when clearly under budget
def store(self, url, body):
    self._write(url, body)
    self._tracked_size += len(body)
    if self._tracked_size > MAX_SIZE:
        self._enforce_size_cap()  # scan only when needed
```

Flag when: an expensive operation (directory walk, sort, full
re-computation) runs unconditionally on every call to a high-frequency
method.

### 3. Triple-Pass Where Single-Pass Suffices

```python
# BAD: iterates refs 3x (once per category)
for ref in refs:
    if ref.startswith("refs/tags/"): ...
for ref in refs:
    if ref.name == target: ...
for ref in refs:
    if ref.name == f"refs/heads/{target}": ...

# GOOD: single pass builds lookup dicts
tags, branches, by_name = {}, {}, {}
for ref in refs:
    by_name[ref.name] = ref
    if ref.name.startswith("refs/tags/"):
        tags[strip_prefix(ref.name)] = ref
    elif ref.name.startswith("refs/heads/"):
        branches[ref.name[len("refs/heads/"):]] = ref
# Then O(1) lookups
```

Flag when: the same collection is iterated multiple times with
different predicates that could all be evaluated in one pass.

### 4. Repeated Environment/Config Parsing

```python
# BAD: re-parses on every call
def classify_host(hostname):
    ghes = os.environ.get("GITHUB_HOST", "").strip().lower().split("/")[0]
    # ... repeated in 5 other functions

# GOOD: parse once in a helper
def _get_ghes_host():
    return os.environ.get("GITHUB_HOST", "").strip().lower().split("/")[0]
```

Flag when: the same `os.environ.get()` + normalisation chain appears
in multiple functions that are called in tight succession.

### 5. Heavy Top-Level Imports on CLI Startup

```python
# BAD: imports entire install engine at module level
from apm_cli.install.pipeline import FullPipeline  # 40+ transitive modules

# GOOD: defer to function scope
def install_command():
    from apm_cli.install.pipeline import FullPipeline
    ...
```

Flag when: a command module imports heavy subpackages at the top level
that are not needed for other commands sharing the same CLI entrypoint.

### 6. Synchronous Blocking in Parallelisable Paths

```python
# BAD: sequential I/O in a loop
for pkg in packages:
    metadata = fetch_metadata(pkg)  # blocking network call

# GOOD: parallel with bounded concurrency
with ThreadPoolExecutor(max_workers=8) as pool:
    metadata_list = list(pool.map(fetch_metadata, packages))
```

Flag when: a loop performs independent I/O operations (file reads,
network calls, subprocess spawns) that have no data dependency between
iterations.

## Scaling Guard Pattern

When reviewing a fix, recommend a scaling-guard test:
- Run the operation at size N and at size 10*N
- Assert the time ratio stays below a threshold (e.g. < 15x)
- This catches O(n^2) regressions without brittle absolute-time assertions

```python
def test_scaling_ratio():
    t_small = median_time(lambda: operation(n=50))
    t_large = median_time(lambda: operation(n=500))
    ratio = t_large / t_small
    assert ratio < 15, f"Ratio {ratio:.1f}x suggests O(n^2)"
```

## Quantification Checklist

When reporting a performance finding, always state:
1. **What** -- the specific pattern (name it from the table above)
2. **Where** -- file:line range
3. **Frequency** -- how often this code path executes per typical run
4. **Complexity** -- the current Big O and the proposed Big O
5. **Fix** -- concrete code sketch (not just "optimise this")
