# T4 upgrade fixtures (0.36.1 red team, resolution and parsing)

Consumer checkouts (apm.yml, apm.lock.yaml, deployed files; no apm_modules)
captured after an older release installed the worlds built by
`tests/utils/skill_selection_world.py::build_upgrade_world`. Commit SHAs are
deterministic (pinned Git identity and dates), so the locks match a freshly
built world.

- `0.35.1/`: v0.35.1 (787a97cc52) source via a PYTHONPATH wrapper, with
  `__BUILD_VERSION__` and `installed_apm_version()` pinned to 0.35.1:
  `.venv/bin/python tests/fixtures/upgrade_0361/t4/generate.py <v0.35.1-wrapper> 0.35.1`
- `0.36.0/`: v0.36.0 (2769573f40, `.venv/bin/apm` on that commit):
  `.venv/bin/python tests/fixtures/upgrade_0361/t4/generate.py .venv/bin/apm 0.36.0`

Scenarios: `divergent-diamond` (first-wins skill_subset), `target-subset`
(first-wins target_subset), `alias` (first-wins alias), `u1-leaked`
(uninstall of an unrelated package left unrecorded skill dirs on disk).
