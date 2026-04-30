---
title: "SSL / TLS issues"
description: "Fix SSL/TLS verification errors when running APM."
sidebar:
  order: 1
---

If `apm install` fails with a TLS error like:

```text
[!] TLS verification failed -- if you're behind a corporate proxy or firewall, set the REQUESTS_CA_BUNDLE environment variable to the path of your organisation's CA bundle (a PEM file) and retry.
```

The most common cause is a corporate TLS-intercepting proxy or firewall (Zscaler, Netskope, Palo Alto, etc.) re-signing HTTPS traffic with an internal CA that APM doesn't trust.

## Fix

Point APM at the PEM file containing your organisation's CA. Ask your IT team for the path if you don't know it.

**Linux / macOS:**

```bash
export REQUESTS_CA_BUNDLE=/path/to/corporate-ca.pem
```

To persist across sessions, add the same line to your shell profile (`~/.bashrc`, `~/.zshrc`, `~/.profile`, etc.).

**Windows (PowerShell):**

```powershell
# Current session only
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corporate-ca.pem"

# Persist for future sessions (user-level)
[Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", "C:\path\to\corporate-ca.pem", "User")
```

## Not behind a proxy or firewall?

The root cause is likely somewhere else. Re-run with `--verbose` for the underlying exception.
