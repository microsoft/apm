---
title: Run a contract
description: Run one packaged job with caller-owned inputs, independent checks, and retained output.
sidebar:
  order: 4
---

Use `apmx`, bundled with APM, to run one contract and assess its output with
named checks. Existing [scripts](../run-scripts/) still use `apm run`.

## Prerequisites

- macOS/Linux, APM with `apmx`, authenticated native Copilot, Git, and Python 3.
- An independent, secret-free caller directory with no Git remote or configured
  policy requirement.

**Native execution is not a sandbox.** Review
[host access and policy limits](../../reference/cli/apmx/#native-execution-boundary).
Never remove remotes or policy to bypass a refusal.

## Run the packaged example

From the APM source checkout root, create a new, persistent caller directory
outside the checkout, then copy the supplied input. The caller must be
independent, with no remote or policy requirement:

```bash
package="$(pwd)/examples/contracts/packaged-job"
caller="$HOME/apmx-contract-example"
mkdir "$caller" &&
cp "$package/caller/notes.md" "$caller/notes.md" &&
cd "$caller" &&
apmx --from "$package" contracts/handoff.contract.md \
  --on copilot --model gpt-6-astra --allow-advisory
```

`mkdir` refuses an existing directory; choose another unused path rather than
reusing it. Choose an accessible model. No caller manifest or install is required.
The package supplies its checker and one self-contained skill; `notes.md` comes
from the caller. Manifests, locks, and global configuration stay unchanged.

## Inspect the result

Follow the reported paths to `.apm/runs/<run-id>/artifacts/` and `record.json`.
There is no automatic copy-back. The checker assesses JSON shape, exact source-ID
coverage, nonempty strings, and the skill's caution prefix, not factual accuracy.
Read the [outcomes](../../reference/cli/apmx/#results-and-retained-files) before
using the artifact.

For offline inspection, replace `--allow-advisory` with `--plan`. A missing skill
or unresolved remote source refuses without fetching. For local-file execution,
use `apmx ./handoff.contract.md --on copilot --allow-advisory`; see the
[source format](../../reference/cli/plan/#contract-source).
