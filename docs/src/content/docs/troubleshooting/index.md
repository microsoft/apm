---
title: Troubleshooting
description: Triage table for the most common APM failures, plus links to deep-dive recovery guides.
sidebar:
  order: 0
---

Something broke. Find your symptom in the table, follow the link.

## Triage by symptom

| Symptom                                                              | Go to                                                              |
|----------------------------------------------------------------------|--------------------------------------------------------------------|
| Don't recognize the error message                                    | [Common errors](./common-errors/)                                  |
| `apm install` failed (auth, network, lockfile, cache, partial)       | [Install failures](./install-failures/)                            |
| `apm compile` exited 0 but no files were written                     | [Compile produced no output](./compile-zero-output-warning/)       |
| `TLS verification failed`, `unable to get local issuer certificate`  | [SSL / TLS issues](./ssl-issues/)                                  |
| `Install blocked by org policy`, rule violation, allowlist mismatch  | [Debugging policy failures](./policy-debugging/)                   |
| Migrating from `awd-cli`, lockfile schema bump, target switch        | [Migration paths](./migration/)                                    |

## When to escalate

If you're past the relevant page and still stuck, gather these and open an issue:

```bash
apm --version
APM_DEBUG=1 apm <failing-command> --verbose 2>&1 | tee /tmp/apm-debug.log
apm cache info
```

Attach the log, your `apm.yml`, your `apm.lock.yaml`, and the `apm cache info` output to the [APM issue tracker](https://github.com/microsoft/apm/issues).

## Related references

- [Environment variables](../reference/environment-variables/) -- everything APM reads from the shell.
- [`apm cache`](../reference/cli/cache/) -- diagnose and recover the local cache.
- [`apm policy`](../reference/cli/policy/) -- inspect what policy applies and why.
