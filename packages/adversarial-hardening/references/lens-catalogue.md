# Lens catalogue - red-team + chaos archetypes (load on demand)

Load this file at the START of a sweep round, when the orchestrator is
about to fan out the adversarial panel (SKILL.md "fan-out" step). Each
lens below is a PERSONA PRELOAD (C2): spawn ONE child thread per lens
on the current head, seeded with the lens charter + the target +
the current fingerprint set (so it skips known classes -- B13). Lenses
are READ-ONLY recon; they return structured JSON findings and write
nothing.

Pick the lenses whose vector-family is plausible for the target; do
NOT fan out all nine on a one-flag surface (cost discipline, A12). A
typical M-surface round runs ~5 lenses.

## Finding return shape (every lens returns this, JSON)

```json
{
  "lens": "<archetype id>",
  "findings": [
    {
      "fingerprint": "<vector-family>:<mechanism>:<surface>",
      "severity": "low|medium|high|critical",
      "vector": "<one-line attack/abuse description>",
      "repro_sketch": "<minimal steps or input that triggers it>",
      "suspected_root_cause": "<where in the target it likely lives>"
    }
  ]
}
```

A lens MUST consult the supplied fingerprint set and OMIT any finding
whose class is already in the ledger (no re-discovery spend).

## A12 gradient inside each lens

Run the CHEAP recon front FIRST: enumerate the attack surface and the
candidate vectors, then DISCARD any vector the enumeration already
clears. Only on a SURVIVING vector do you spend effort authoring an
actual probe. This is a SEQUENCING discipline, not a model switch -- it
holds on whatever model the harness gives the child thread, because the
saving comes from not authoring probes against already-cleared surface.

[i] PER-LENS MODEL ROUTING (B12): cross-lens routing -- e.g. a
stronger model for security red-team lenses than for a chaos smoke
pass -- binds on each lens's CUSTOM-AGENT profile, never in this
catalogue and never in SKILL.md. On Copilot (the target harness) the
binding site is the `.agent.md` `model:` field, mirroring how the
repo's `../../agents/*.agent.md` specialists set `model:`; skill
frontmatter cannot carry `model:`. A lens spawned WITHOUT a dedicated
agent profile runs at the session default model -- still correct, just
unrouted. Never make a finding's correctness depend on which model ran
the lens, and never name a role class the runtime cannot bind.

---

## Red-team lenses (security)

### RT-1 input-boundary
Untrusted input crossing a trust boundary: argv / env / stdin / config
/ file paths / network responses. Vectors: injection (argv, shell,
path traversal, format string), unvalidated deserialization,
encoding/normalization confusion, oversized or malformed input.

### RT-2 resource-exhaustion
Inputs or call patterns that make the target consume unbounded CPU,
memory, file descriptors, disk, or stack. Vectors: unbounded
recursion / loops on attacker-influenced size, quadratic blowups,
zip/expansion bombs, missing depth or size caps.

### RT-3 state-and-concurrency
Shared mutable state under interleaving or re-entrancy. Vectors: TOCTOU
races, partial/torn writes, non-atomic file or lockfile updates,
double-free / double-close, ordering assumptions that break under
parallelism.

### RT-4 trust-and-authz
Where the target decides what is allowed. Vectors: missing or
bypassable authorization checks, confused-deputy, privilege or scope
escalation, token/credential over-scoping or leakage into logs,
trusting a value that crossed a boundary.

### RT-5 dependency-supply-chain
How the target resolves, fetches, and trusts external code/data.
Vectors: dependency confusion, unpinned or mutable refs, missing
integrity/signature verification, typosquat-prone resolution, install
hooks executing untrusted code. (NOTE: hardening the target's OWN
resolution is in scope; BUILDING a malware/secret scanner is OOS-1.)

---

## Chaos / resilience lenses

### CH-1 fault-injection
Inject failures at every external dependency the target calls: I/O
errors, ENOSPC, permission denied, killed subprocess, corrupted
response. Question: does the target fail SAFELY (no partial commit, no
corrupted state, clear error) or does it leave wreckage?

### CH-2 latency-and-timeout
Slow or hanging dependencies. Vectors: missing timeouts, unbounded
waits, retry storms with no backoff, deadlocks under slow I/O,
no-progress hangs. Question: does the target bound its waits and
degrade gracefully?

### CH-3 concurrency-storm
Many simultaneous invocations / signals against the same resource.
Vectors: lock contention, cache stampede, duplicate-effect on retry,
non-idempotent operations replayed. Question: is the operation safe to
run concurrently and to retry?

### CH-4 exhaustion-and-limits
The target at the edge of its operating envelope: full disk, hit
rate-limit, max open files, memory pressure, truncated environment.
Question: are limits detected and surfaced, or does the target corrupt
state / crash opaquely?

---

## Charter gate reminder

Every finding these lenses return is a CANDIDATE only. The
charter-arbiter dedups it against the ledger fingerprints and gates it
against the ratified charter (Sections 2-5). A finding whose root
cause is OUT of scope is DECLINED with the clause id -- the lens does
not get to decide scope. A leaked third-party token (RT-4/RT-5
shaped) is the canonical DECLINE under `OOS-1`, not a fix to build.
