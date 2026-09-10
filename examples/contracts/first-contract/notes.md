# APM teammate handoff

- restore: After cloning an APM project, run apm install to restore its declared dependencies. Review executable content before running it.
- files: apm.yml declares dependencies and apm.lock.yaml records resolution. apm_modules/ is installation storage, not portable source syntax.
- scripts: Bare apm run selects scripts.start. Without start it exits 1 and lists the available scripts; it never discovers every contract.
