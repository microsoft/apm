# Run a packaged handoff

This package selects one explicit contract, one package-owned checker, and one
self-contained local skill. The checker and skill reuse the original
`first-contract` / `handoff-style` examples. No install step is required:
`apmx` prepares the declared skill privately, without deploying it to your
project or changing this package.

From the repository root, with `apmx` installed and Copilot authenticated:

```sh
apm experimental enable contracts
package="$(pwd)/examples/contracts/packaged-job"
caller="$HOME/apmx-contract-example"
mkdir "$caller" &&
cp "$package/caller/notes.md" "$caller/notes.md" &&
cd "$caller" &&
apmx --from "$package" contracts/handoff.contract.md \
  --on copilot --allow-host-access
```

Choose an unused caller path outside this checkout and any repository with a
remote or configured policy. `mkdir` deliberately refuses an existing path;
the command chain stops rather than reusing or overwriting an existing caller.

The caller does not need `apm.yml`. Edit the copied `notes.md` to supply your own
facts. The package's top-level `notes.md` is a deliberate conflicting sentinel:
inputs come from the caller, not the package.

`handoff.json` and its record remain below the caller's `.apm/runs/` directory.
There is no automatic copy back to the caller root. Inspect the retained record
and artifact before using the result. A completed run with passing checks still
returns `UNPROVEN` (exit 21), because native execution is not sandboxed.
The output was saved and checked; the overall result does not turn a passing
check into a failure. These checks do not establish complete factual
correctness of the generated prose.

For a read-only local preview, replace `--allow-host-access` with `--plan`.
An unresolved remote source or missing import may remain unproven offline;
planning never fetches it or runs inference.

The acceptance tests use an explicitly **hermetic Copilot protocol fixture**,
not live inference. A real native run of the command above is a separate
acceptance step. To isolate native configuration, use Copilot's supported
invocation-local `COPILOT_HOME` profile; do not copy credentials or edit global
configuration for this example.
