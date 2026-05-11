---
title: "SSL / TLS issues"
description: "Diagnose and fix TLS verification failures during apm install and apm audit."
sidebar:
  order: 4
---

`apm install` and `apm audit` reach out to GitHub, GHES, GitLab, Azure DevOps, and package archives over HTTPS. When the system can't verify the server certificate, the operation fails. This page maps the failure modes to fixes.

Related: [environment variables](../reference/environment-variables/), [install failures](./install-failures/), [security and supply chain](../enterprise/security-and-supply-chain/), [authentication](../enterprise/authentication/).

## Symptoms

Typical errors APM surfaces or passes through from the underlying HTTP/git stack:

```text
[!] TLS verification failed -- if you're behind a corporate proxy or
    firewall, set REQUESTS_CA_BUNDLE to your organisation's CA bundle.
```

```text
SSLError: HTTPSConnectionPool(host='api.github.com', port=443):
  Max retries exceeded ... [SSL: CERTIFICATE_VERIFY_FAILED]
  certificate verify failed: unable to get local issuer certificate
```

```text
fatal: unable to access 'https://github.example.com/...':
  SSL certificate problem: self-signed certificate in certificate chain
```

```text
fatal: unable to access '...': server certificate verification failed.
  CAfile: none CRLfile: none
```

All of these mean the same thing: the TLS chain presented by the server can't be validated against the trust store APM is using.

## First diagnostic

Decide which of the three categories you are in before changing anything:

[*] **Corporate TLS-intercepting proxy** (Zscaler, Netskope, Palo Alto, Cisco Umbrella, Blue Coat). The server cert is re-signed by an internal CA. Affects every HTTPS host. Fix: trust the corporate CA.

[*] **Self-hosted server with internal CA** (GHES, GitLab self-managed, internal artifact host). Only that one host fails; public hosts like `api.github.com` work fine. Fix: trust the internal CA, often per-host.

[*] **Genuine certificate problem** (expired, wrong hostname, broken chain). Reproduce with `curl -v https://<host>` from the same shell. If `curl` also fails, the problem is upstream of APM.

Re-run the failing command with `--verbose` to see the underlying exception and the host that triggered it:

```bash
apm install --verbose
```

## Configure trust

APM uses `requests` for HTTP and shells out to `git` for repository operations. Both honour standard environment variables. Set them at the shell or in your profile (`~/.zshrc`, `~/.bashrc`, or the Windows user environment).

### Python HTTP layer

```bash
export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem
# or, more general:
export SSL_CERT_FILE=/path/to/ca-bundle.pem
export SSL_CERT_DIR=/etc/ssl/certs
```

`REQUESTS_CA_BUNDLE` wins for `requests`. `SSL_CERT_FILE` / `SSL_CERT_DIR` cover the rest of the Python TLS stack.

### Git operations

```bash
export GIT_SSL_CAINFO=/path/to/ca-bundle.pem
```

For one host only, prefer per-host git config so you don't widen trust globally:

```bash
git config --global http.https://github.example.com/.sslCAInfo /path/to/internal-ca.pem
```

The trailing slash matters - it scopes the setting to that origin.

### Windows (PowerShell)

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\certs\corporate-ca.pem"
$env:GIT_SSL_CAINFO     = "C:\certs\corporate-ca.pem"

# Persist for the current user:
[Environment]::SetEnvironmentVariable("REQUESTS_CA_BUNDLE", "C:\certs\corporate-ca.pem", "User")
```

### Where do I get the CA file?

Your IT or platform team owns it. Ask for the PEM bundle for the proxy or internal PKI. Do not export it yourself from a browser unless that is the documented procedure - you may capture an intermediate, not the root.

## GHES and GitLab self-managed

Trust alone is not enough for self-hosted forges. APM also needs to know which host to talk to.

**GHES:**

```bash
export GITHUB_HOST=github.example.com
export GITHUB_APM_PAT=<token>
export GIT_SSL_CAINFO=/path/to/internal-ca.pem
```

**GitLab self-managed:**

```bash
export GITLAB_HOST=gitlab.example.com
export APM_GITLAB_HOSTS=gitlab.example.com,gitlab-eu.example.com
export GITLAB_APM_PAT=<token>
export GIT_SSL_CAINFO=/path/to/internal-ca.pem
```

See [environment variables](../reference/environment-variables/) for the full list and [authentication](../enterprise/authentication/) for token scopes.

## Proxies

APM does not implement its own proxy logic. It honours the standard variables, which `requests` and `git` both read:

```bash
export HTTPS_PROXY=http://proxy.example.com:8080
export HTTP_PROXY=http://proxy.example.com:8080
export NO_PROXY=localhost,127.0.0.1,.internal.example.com
```

If the proxy performs TLS interception, you also need the proxy's signing CA in the trust store - see [Configure trust](#configure-trust). Importing the CA into the OS trust store (Keychain on macOS, `update-ca-certificates` on Debian/Ubuntu, `update-ca-trust` on RHEL, the Trusted Root store on Windows) is the most durable fix; consult your OS documentation rather than copying steps from here.

## Verify the fix

```bash
# Python side
python -c "import requests; print(requests.get('https://api.github.com').status_code)"

# Git side
GIT_CURL_VERBOSE=1 git ls-remote https://github.example.com/org/repo.git 2>&1 | grep -i 'ssl\|cert'

# APM end-to-end
apm install --verbose
```

A `200` from `requests`, a successful `ls-remote`, and a clean install confirm trust is wired through every layer APM uses.

## Development-only escape hatches

:::caution[Development only]
The settings below disable certificate verification. They expose every request to trivial man-in-the-middle attacks and **must never be used in CI, on shared machines, or against production data**. Trusting the right CA is always the correct fix.
:::

If you are isolated on a laptop, debugging a local server with a self-signed cert, and you accept the risk:

```bash
export GIT_SSL_NO_VERIFY=true       # git only
export PYTHONHTTPSVERIFY=0          # Python stdlib only; requests ignores this
```

What you lose: any guarantee that the host you reached is the host you intended to reach. Tokens you send may be captured. Packages you download may be tampered with - APM's [built-in security scanning](../enterprise/security-and-supply-chain/) still runs on the bytes received, but it cannot detect substitution upstream of itself.

Unset both as soon as you are done:

```bash
unset GIT_SSL_NO_VERIFY PYTHONHTTPSVERIFY
```

## Still failing?

[>] Re-run with `--verbose` and capture the full exception chain.
[>] Check `curl -v https://<host>` from the same shell - if it fails, the problem is the system trust store, not APM.
[>] Confirm `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` point at a readable PEM file (`openssl x509 -in $REQUESTS_CA_BUNDLE -noout -subject` should print a subject line).
[>] If only one host fails, see [GHES and GitLab self-managed](#ghes-and-gitlab-self-managed) and the per-host `git config` recipe above.
[>] If the install proceeds past TLS but then fails, continue at [install failures](./install-failures/).
