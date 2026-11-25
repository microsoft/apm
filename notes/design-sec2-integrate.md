# Section 2: Integrate redesign (v0.40 headline)

Owner: PERF-BETS. Status: DRAFT for W3d co-sign. All numbers are cold tree-50, paired CPU incl. children, against the base named in each row.

## 2.0 Goal and bar

- Integrate is 50% of W cold (6.5 s) and 45% of L cold (2.7 s) on main 1e4a208e (SoL split; W runs default Git for Windows / schannel).
- Kill bar: integrate CPU <= 50% of today, which is -3.3 s on W and -1.35 s on L.
- No single family is big enough on its own:
  - skills 14.6% of W;
  - the shared per-file pipeline 18.8% of W / 19.6% of L;
  - containment is the W cost driver (6.2 s of 16.8 s in the W profile).

## 2.1 Security invariant (W3d terms) -- stated before any shape

The redesign must be NO WEAKER than today's per-path containment. In W3d's `containment-once` terms (K1-K9):

- **I1 (by construction).** A destination or source tree that APM created in THIS invocation, with exclusive `mkdir` and no-follow `CREATE_NEW` writes, holds no link that APM did not write. That is K2. Its paths need no per-path resolve.
- **I2 (one resolve per root).** Every other tree (deploy roots such as `.github/` and `.claude/`, the project root, and trees from earlier runs) is proven link-free ONCE per command. The proof is a full resolve of the root plus an lstat walk of the components the plan will use. It lives in `invocation_memo` (K1) and is dropped:
  - by `forget_invocation_memos` after any user code (lifecycle scripts, hooks);
  - by `release_held` -> clear-all forget before any APM rename, delete or link creation (R2/R4, as in #5382 c8eb343f).
- **I3 (leaf refusal).** Under a proven root, each file write is `CREATE_NEW` / `O_NOFOLLOW` for a new leaf. For an existing leaf: lstat-refuse a link or reparse point, then atomic replace (K3/K5, never in place). Each new directory component is created exclusively and proven before first use (K4). Per-file cost is O(1): one open, plus one lstat when replacing.
- **I4 (lexical relpaths only under a proof).** Locators, receipts and relative paths take the lexical form only when the proof saw no in-root link, so lockfile bytes are unchanged (K6). Otherwise they use the full resolve, as today.
- **I5.** Exact-text keys (K7). One owner, `utils/realpath.py` plus the new `install/package_plan.py`, with an arch guard (K8). A differential covers mid-install user code and a warm planted link (K9).
- **Out of model** (as ruled): a concurrent same-user process racing the command.

### Map: today's per-path checks -> replacement (counts from the L profile of main, run 45523400455)

| today's check | calls / tree-50 | where | replacement | invariant |
|---|---|---|---|---|
| `ensure_path_within_resolved` + `has_symlink_component` per source file | 2067 | `deployable_source_plan._is_safe_source_path` (`safe`, `copy_ignore`) | One no-follow walk per package at plan time records the link-free file set. A tree created this invocation (in-process fetch / checkout landing) is link-free by construction (I1). An earlier-run tree is walked once (I2). Per file: a set lookup. | K2, I1/I2 |
| `ensure_path_within` + `has_symlink_component` (security gate scan) | 667 | `security/gate._explicit_scan_candidate` | Reads the same plan file set; the scan still reads every byte it scans. | K2, K3 |
| `ensure_path_within` (destination) | 650 | `copy_directory_files`, `integrate_{instructions,prompts,agents}_for_target` | Deploy root proven once (I2); per file a lexical join under the root, then I3 (CREATE_NEW / lstat-refuse + atomic replace). | K3/K4/K5 |
| `validate_deploy_path` + `has_symlink_component` | 200 | `skill_preflight.find_unmanaged_conflicts` | The same root proof plus a leaf lstat (a conflict check is a read). | I2, I3 |
| `resolve_for_containment` (ledger locators) | 3618 + 2734 | `deployment_ledger.locator_for_path`, `deployed_paths._resolved_candidate` | Locator = the plan's lexical rel under a proven root (I4); full resolve when unproven. | K6 |
| `resolve_path` via `portable_relpath(s)` | 5652 | receipts, lockfile `deployed_files` | Same as above (I4). | K6 |
| `resolve_path` in link resolver / skill roots / globbing | ~3000 | `init_link_resolver`, `_inactive_skill_roots`, `find_files_by_glob` | Computed once per package in the plan (Shape A). | K1 |

### Receipts and ownership (unchanged contracts)

- **Receipts.** Every deployed file is still recorded with its content hash in the lockfile and deployment receipts. The hash is computed from the bytes written (for a staged skill tree, in one pass over the staged tree). The differential (2.5) proves the lockfile and receipts are byte-identical.
- **Ownership.** A destination is written only when it is absent or APM-owned per the deployment ledger. That is checked against the plan before any write (today's `check_before_deploy` / `find_unmanaged_conflicts`, same semantics). A skill tree swap requires the destination directory to be absent or ledger-owned in full.
- **Deploy journal.** Unchanged per file for non-skill primitives. A staged skill tree is one `dir` unit (`deploy_snapshot` already supports `dir` units with `_tree_hashes`). Replay semantics are unchanged.

## 2.2 Shapes

### A. One `PackagePlan` per package (shared pipeline)

- Computed once at materialization, consumed by every integrator:
  - package type, primitive inventory (one walk) and link-free file set;
  - parsed `apm.yml`;
  - link-resolver contexts;
  - package content hash;
  - PARSED primitives (one parse per file, see 2.4).
- Today these are re-derived 2-5x:
  - `detect_package_type` and `init_link_resolver`: 250 calls each (5 integrators x 50 packages);
  - `validate_apm_package` and `compute_package_hash`: 100 each;
  - 3 inventory walks per package;
  - `loads_frontmatter`: 950 calls, ~19 per package.
- **Cost line, per package:** 1 walk (was 3), 1 detect (was 5), 1 hash (was 2), 1 parse per primitive file (was ~3-4). Spawns 0.

### B. Bulk deploy: skill bundles as one staged tree

- Reflink copytree into a stage, one tree hash, one atomic rename, one `dir` journal unit. Containment by construction (I1).
- **Cost line, per skill:** 1 copytree + 1 rename + 1 tree hash (was N writes + 2N journal events + N containment checks).

### C. Containment by construction for destinations

- I1-I4 above, applied to every integrator's writes.
- This is the W lever. It replaces today's per-path resolve; it does not remove the check.

## 2.3 Measured ceilings (research upper bounds, deliberately unsafe; ceilings only)

Base main 1e4a208e:

| head | W | L |
|---|---|---|
| A memo (576929fa) | 0.970, -0.9 s [45524106186] | 0.976, -0.15 s [45524085463] |
| A + journal off (532d7a1f) | 0.969 [45525632628] | 0.936 [45525615896] |
| A + lexical resolve / no link scan (893b7b89) | 0.752 [45525667777] | 0.947 [45525650620] |
| A + both (ea46241b) | 0.688, -5.8 s [45524926467] | 0.901, -0.54 s [45524908282] |
| A + both + parse-once (4c98c85e) | - | 0.914, 6.96 -> 6.14 s, -0.82 s [45526633274]; same-window rerun of A + both: 0.920, -0.45 s [45526647663] |

Base main + #5374 (cc5aac78; the W claim requires this row, per VP):

| head | W |
|---|---|
| A memo (1b0a0ead) | 0.975 [45526483374] |
| A + lexical/no link scan (f91d724d) | 0.793 [45526498364] |
| A + both (8efedece) | 0.718, 15.36 -> 10.91 s, -4.45 s [45526516043] |

Reading:
- W on main + #5374 still clears the bar: -4.45 s vs -3.3 s. #5374 does not remove the containment share; containment by construction is the W lever.
- L misses the bar even with parse-once: best -0.82 s vs -1.35 s (about 30% of L integrate). L <= 50% needs B (skill trees as one op) plus cheaper copies/writes, measured next.
- On W, the containment family carries the win (0.970 -> 0.752). The journal is ~0 on W.
- On L, the journal (-4%) and resolve (-3%) are both small. L's remainder is content transforms: frontmatter re-parse, `write_text_lf` and copies, about 0.8 s real.

## 2.4 Parse-once: one parsed model per primitive, N renders

- **Model.** `PackagePlan.primitives[path] = ParsedPrimitive(metadata, body, raw_bytes_hash)`, parsed once with the bounded loader (`yaml_io.loads_frontmatter`, same hostile-input limits).
- Each integrator x target RENDERS from the model, never re-reading or re-parsing the file.
- Renders must not mutate the shared model: integrators receive a read-only view; any per-target change copies.
- **Invariant.** The model is keyed by exact path text plus the content hash of the bytes read at plan time. The plan is dropped with the invocation memos (user code), and on any APM write to the source tree (release_held forget).

## 2.5 Acceptance differential (byte-identical render)

- **Harness.** `perf-research/arch-render-diff`, `.github/probe/render_diff.py`. In each offline fixture world, cold-install with the BASE CLI and with the CANDIDATE CLI. Then snapshot every file under the project, excluding `apm_modules/` and `.git/`:
  - deployed primitives for every target;
  - MCP configs;
  - hooks;
  - the lockfile minus timestamp lines;
  - deployment receipts.
- It compares sha256 per path, and fails on any added, missing or differing path.
- **Fixtures:**
  - `tree-50`: instructions, prompts, agents, skills, sub-skills, diamonds;
  - `monorepo-4`: several `apm.yml` roots;
  - `tree-10-mcp`: a stdio MCP server per package;
  - `tree-10-multi`: GitHub, GHE Cloud, GitLab and ADO hosts.
- Extend with `-policy` variants for discovery/enforcement and a Windows run for CRLF/`core.autocrlf` paths.
- **Cells (the product PR's tests).**
  1. Differential over the fixture list above on L, W and M.
  2. A primitive whose frontmatter one integrator would mutate is rendered identically for a second target (copy-on-write view).
  3. A source file changed by user code mid-install (lifecycle script) is re-parsed (memo dropped).
  4. Hostile frontmatter (alias bomb, `<<` merge) still refuses through the bounded loader, parsed once.
- **Results:**
  - A/A (main vs main, run 45526809921): `.apm-local/installation` (a per-install id) differs in every fixture, and the 4 monorepo app lockfiles differ (side-dependent content). These are harness noise; the harness must exclude them.
  - Parse-once upper bound vs main (run 45526789209): EXACTLY the same diff set as A/A and nothing else. Every deployed primitive, MCP config, hook and receipt is byte-identical, as are the tree-50/mcp/multi lockfiles.
  - Harness TODO: exclude `.apm-local/installation` and explain the monorepo lockfile delta, so IDENTICAL means zero.

## 2.6 Build order and gates

1. A (plan plus parse-once). Gate: differential IDENTICAL, L+W+M cold t50 benches.
2. C (destination containment by construction). Gate: W3d review of the 2.1 map, the K9 differential, and the W bench on main + #5374.
3. B (skill trees as one op). Gate: differential, receipts identical, and a journal replay cell.
