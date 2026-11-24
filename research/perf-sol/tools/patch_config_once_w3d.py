from pathlib import Path

p = Path("src/apm_cli/utils/git_env.py")
s = p.read_text()

old = '''def _has_include(snapshot: _GitConfigSnapshot) -> bool:
    for entry in snapshot.entries:
        key = entry.key.lower()
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            return True
    return False
'''
new = '''def _needs_probe_repository(snapshot: _GitConfigSnapshot) -> bool:
    """Whether the repository-less snapshot may differ from the probe repository's.

    W3d D1/D2: an include or includeIf in any scope can select files by the
    repository, and any local/worktree entry (the probe ran inside some
    repository) is not what a fresh probe repository holds.
    """
    for entry in snapshot.entries:
        if entry.scope in {"local", "worktree"}:
            return True
        key = entry.key.lower()
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            return True
    return False
'''
assert old in s
s = s.replace(old, new)
s = s.replace(
    "    if _has_include(parent) or _has_include(child):\n        return None\n",
    "    if _needs_probe_repository(parent) or _needs_probe_repository(child):\n        return None\n",
)
old = '''    (selected,) = best_keys
    return tuple(entry for entry in headers if entry.key.lower() == selected)
'''
new = '''    (selected,) = best_keys
    group = tuple(entry for entry in headers if entry.key.lower() == selected)
    if len({entry.key for entry in group}) != 1:
        return None  # W3d D4: case-folded spellings of one key (path case): let Git decide
    return group
'''
assert old in s
s = s.replace(old, new)
old = "_CONFIG_ONCE_DIFFERENTIAL = False\n"
new = (
    "# W3d D7: the differential (both paths, raise on mismatch) runs under APM_DEBUG.\n"
    "_CONFIG_ONCE_DIFFERENTIAL = bool(os.environ.get(\"APM_DEBUG\"))\n"
)
assert old in s
s = s.replace(old, new)
p.write_text(s)
print("patched")
