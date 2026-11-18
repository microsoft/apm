# T1 (auth) upgrade fixtures

`0.35.1/` was produced by the `v0.35.1` tag (787a97cc52) and `0.36.0/` by the
v0.36.0 release commit (2769573f40), each with
`PYTHONPATH=. python tests/fixtures/upgrade_0361/t1/generate.py <apm> <version>`
against the hermetic fixtures in `tests/integration/_ado_auth_kit.py` (ADO
dependency installed with `ADO_APM_PAT`) and
`tests/integration/test_auth_upgrade_lock_replay_lifecycle.py` (registry
dependency with `resolved_url`). Consumed by
`test_auth_upgrade_lock_replay_lifecycle.py`.
