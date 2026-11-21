from pathlib import Path
p = Path("src/apm_cli/install/dry_run_plan.py")
s = p.read_text()
s = s.replace("from apm_cli.drift import locked_marketplace_row\n", "from apm_cli.drift import locked_marketplace_replay\n")
old = '''def _resolved_key(dependency: DependencyReference, lockfile: LockFile | None) -> str:
    """Return the key the resolver records for one manifest declaration.

    A locked object-form ``{name, marketplace}`` declaration replays to its
    lock row's coordinate (#3960), so the graph holds it under that key.
    """
    locked = locked_marketplace_row(dependency, lockfile)
    return (locked or dependency).get_unique_key()
'''
new = '''def _resolved_key(
    dependency: DependencyReference,
    lockfile: LockFile | None,
    *,
    update: bool,
    apm_modules_dir: Path,
) -> str:
    """Return the key the resolver records for one manifest declaration.

    Asks the resolver's own replay owner: a replayed object-form
    ``{name, marketplace}`` declaration sits in the graph under its lock
    row's coordinate (#3960); any other declaration keeps its own key.
    """
    decision = locked_marketplace_replay(
        dependency, lockfile, update_refs=update, modules_dir=apm_modules_dir
    )
    return (decision.reference or dependency).get_unique_key()
'''
assert old in s
s = s.replace(old, new)
old2 = "            if _resolved_key(dependency, lockfile) not in resolved_keys\n"
new2 = """            if _resolved_key(
                dependency, lockfile, update=update, apm_modules_dir=apm_modules_dir
            )
            not in resolved_keys
"""
assert old2 in s
s = s.replace(old2, new2)
p.write_text(s)

p = Path("src/apm_cli/install/template.py")
s = p.read_text()
old = "                # Not resolved by the offline preview: nothing materialized to inspect.\n"
new = ("                # The read-only preview resolves a marketplace declaration only by\n"
       "                # lock replay; one left unresolved has no install path and its\n"
       "                # preview error is already reported.\n")
assert old in s
p.write_text(s.replace(old, new))

p = Path("scripts/architecture_linter/checks/marketplace_locked_evidence.py")
s = p.read_text()
old = '    ("src/apm_cli/install/dry_run_plan.py", "_resolved_key", "locked_marketplace_row"),\n'
assert old in s
s = s.replace(old, "")
anchor = '    ("src/apm_cli/drift.py", "_owns_locked_servers", "mcp_package_owner"),\n'
assert anchor in s
s = s.replace(anchor, anchor + '    ("src/apm_cli/install/dry_run_plan.py", "_resolved_key", "locked_marketplace_replay"),\n')
p.write_text(s)
