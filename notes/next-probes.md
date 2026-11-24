# Next probes (PERF-BETS)

## P2 MATERIALIZATION FAN-OUT: writes by kind x caller (population: all; f251f785, PRE-#5333; cold t50)
- Harness: perf-research/arch-writes-attr @ 00e10261. It is arch-n1-attr plus classification of every open-for-write, os.open(O_CREAT|W), mkdir, replace, link and unlink by path kind (cache/<sub>[/.git], apm_modules, target:<root>, journal store, +tmp) and innermost apm caller.
- Runs: L 45386020949, W 45386043073 (base == new == f251f785, PROFILE=1).
- Totals: L 11,138 write ops = 223/pkg (mkdir 3,623, os.open-w 3,345, open-w 2,981, replace 1,171). W 10,411.
- On Windows, shutil.copy2 uses CopyFile2, so the 717 apm_modules copies are not seen as opens there.
- Each package carries ~14.3 payload files and deploys 13.3 files (667 = 467 .github + 200 .agents) to ONE target (copilot). So per-target fan-out is 1x here: the excess is NOT targets.

| # | stage (caller) | kind | ops L (per pkg) | what it is | physics floor | verdict |
|---|---|---|---|---|---|---|
| 1 | inprocess_fetch._write_git_dir / _write_file | cache .git | 1,434 files + 1,574 mkdir = 3,008 (60) | B1 writes every reached object LOOSE (zlib each) + 256-fan dirs + refs dirs | pack + idx = 2 files + ~4 dirs (~6) | WASTE ~54/pkg: write the received pack + a v2 idx instead of loose objects |
| 2 | inprocess_fetch._write_file / fetch_worktree, git_cache._inprocess_checkout | cache worktree | 717 files + 717 mkdir + 50 (~30) | payload checkout in the cache | ~14 files + ~10 dirs | physics (cross-project cache) |
| 3 | file_ops._reflink_copy_file / _do_copytree | apm_modules | 717 files + 467 mkdir (24) | SECOND full copy of the payload, cache -> apm_modules (ext4/NTFS: no reflink -> byte copy) | link per file (same bytes) | WASTE in bytes: hardlink/reflink from the verified cache checkout |
| 4 | deploy_store.put / _prepare / put_record | cache deploy_v1 | 717 tmp + 717 replace + 280 mkdir = 1,714 (34) | every deployed file's bytes written AGAIN into the CAS deploy store, tmp + rename | 0 extra writes if the store entry is a link to the just-written target (or vice versa) | WASTE ~34/pkg (link or defer) |
| 5 | atomic_io.write_text_lf / atomic_copy, write_seam.mkdir | target .github/.agents | 467 + 200 tmp + 200 replace + 206 mkdir = 1,073 (21) | the deploy itself; skills use tmp + rename even on a fresh dest | 667 files + ~200 dirs (17) | ~4/pkg waste: O_EXCL direct on a fresh dest (no tmp + rename) |
| 6 | deploy_snapshot._append_line | journal store | 1,795 opens (36) | open per journal line | 1 handle | WASTE: S5 (already in arch-n1-hack) |
| 7 | commit_reachability._write_marker | cache reachability | 152 mkdir + 50 tmp + 50 replace (5) | per-pkg marker, 3 dirs each | 1 batched file per run | small waste |
| 8 | remember_resolved_ref, cache_pin._replace_marker, locking.atomic_land, resolution_staging, download_package, file_ops lambda | cache/apm_modules markers | ~550 (11) | per-pkg markers + staging rename dirs | ~3/pkg | small waste |
| 9 | W only: git_auth_env.isolated_global_config_path | elsewhere | 101 open-w (2) | isolated gitconfig per git call | 1 per command | small waste |
- Floor with today's architecture (cache + apm_modules + one target): ~45-50 ops/pkg vs 223 measured.
- The excess is ~170 ops/pkg. Of that, 1 (loose objects) ~54, 4 (deploy-store double write) ~34, 6 (journal opens) ~36, 3 (payload copy) ~24 as bytes, 5 + 7 + 8 + 9 ~20.
- CPU bound (SoL F7 clean attribution, same SHA): family d copies/writes is L 0.45 s (4.9%) / W 1.06 s (10.6%), and e journal is 0.37 s (4.0%) / 0.62 s (6.2%). B1's zlib + loose writes sit in the fetch rows (d + part of g).
- So the WHOLE write fan-out is <= ~9% L / ~17% W of cold t50 even at zero writes. It is a Windows-leaning lever; Linux is likely below the 10% bar.

### W3d ruling w3d:ruling:fanout (received)
- Q1 store-entry hardlink: NO. A shared inode corrupts the rollback copy, and nlink=2 trips base_integrator:1631 and config_file_provenance:534. Use a reflink, with a fallback that writes the store copy from the same buffer (today's 1 write). So row 4 is PHYSICS on ext4/NTFS and deletable only on reflink filesystems (APFS/btrfs/XFS).
- Q2 payload hardlink: NO, physics (same guard). Reflink already exists.
- Q3 pack + idx: YES, no re-pack, with K1-K6:
  - K1: hash every object, failure refuses;
  - K2: the idx is built only from in-process hashes, trailer + CRCs;
  - K3: no thin packs, .keep or .promisor;
  - K4: the pack cap bounds the bloat;
  - K5: git fsck --full --strict cell + checkout_verify parity cell;
  - K6: no sha1dc residual.
- Q4 fresh-dest O_EXCL: YES on POSIX, with W1 durable pending record, W2 non-existing dests only, W3 fstat size, W4 Windows keeps rename, W5 crash + EEXIST cells.
- Benches:
  - UPPER (unfaithful; rows 1 + 4 + 6): arch-writes-hack 2356d528. Runs L 45387129877, W 45387176531; profile 45387218040.
  - FAITHFUL (rows 1 + 6; store kept; idx write is an idx-sized dummy, and the real v2 idx is in-memory sort + crc32, sub-ms/pkg): arch-writes-faithful bead9b9f.

### P2 results (vs f251f785, PRE-#5333; cold; 6 pairs; paired CPU incl. children)
| variant | L t50 | L t10 | W t50 | W t10 | runs |
|---|---|---|---|---|---|
| UPPER, unfaithful (rows 1 + 4 + 6), 2356d528 | 11.87 -> 11.09 s, 0.944 | 0.981 | 21.11 -> 20.06 s, 0.951 | 0.948 | L 45387129877, W 45387176531 |
| FAITHFUL (rows 1 + 6, store kept), bead9b9f | 12.80 -> 12.39 s, 0.964 | 0.975 | 21.07 -> 20.11 s, 0.948 | 0.961 | L 45387750512, W 45387773859 |
- Engagement (profile 45387218040, upper): write ops 11,138 -> 5,458 (-51%); .git 3,008 -> 600; deploy_v1 1,714 -> 235; journal 1,795 -> ~3 opens.
- VERDICT: halving the write ops buys 3.6% L / 5.2% W (faithful) and <= 5.6% even unfaithful. Below the 10% bar, so KILLED as a bet.
- Writes are not the cost on runners: ~0.08 ms per op L. The remaining "deploy" CPU is in-process Python work (containment, YAML, discovery), not syscalls.
- Residue (stacking cars, faithful under w3d:ruling:fanout):
  - Q3 pack + idx: -54 ops/pkg, needs K1-K6 cells;
  - S5 journal handle.
  - Together ~4-5% W, ~3.5% L. Kept on perf-research/arch-writes-faithful.

## P1 GC probe (population: all; f251f785 PRE-#5333; cold)
- Change (perf-research/arch-gc @ 1de99c56): gc.set_threshold(100_000, 50, 50) at the top of entrypoint.main. 3 lines, no security surface.
- Collections at t50 L (harness arch-writes-attr 48a58fa7, gc.callbacks):
  - base 685 (gen0 623 / gen1 57 / gen2 5), 0.307 s in GC (gen2 0.221 s) [45387611027];
  - new 39 (36 / 3 / 0), 0.065 s [45387636751].
- Bench, 6 pairs:
  - L t50 7.75 -> 7.49 s, paired 0.985; t10 0.994 [45387568098].
  - W t50 15.16 -> 15.16 s, paired 0.993; t10 0.992 [45387588614].
- VERDICT: ~1.5% L / ~0.7% W, consistent with the 0.24 s of GC removed. Too small as a bet. It is a free, safe stacking car, though peak RSS was not measured.

## P3 GIT-PATH FETCH SHAPE (population: all; on main a235f590 every fetch is git CLI, B1 not on main) -- CLOSED
- Today: git_cache._ensure_bare_repo cold miss does `clone --bare --no-tags`, which brings every branch's full history (+ blob:none for subdir deps).
- W3d w3d:ruling:fetch-depth1 = OK with D1-D8.
- v1 bffefcf7 (init + config + fetch --depth=1 <sha>):
  - tree-50 L 1.222 / W 1.540 (spawns 672 -> 1,324, connects 55 -> 105) [45391806202 / 45391824868].
  - real L 0.985 / W 0.935.
- v2 4f1d5876 (+ pin ref + HEAD):
  - L real 7.20 -> 18.48 s, paired 2.568 (wall 6.0 -> 130.9 s); tree-50 1.115 (spawns 673 -> 1,024) [45394357668]. W [45394378611].
  - Partial + depth-1 + shallow makes the sparse checkout lazy-fetch blobs one by one.
- VERDICT: CLOSED. Nothing reads <= 0.90 on the real-history fixture, and the git child CPU there is `git checkout` (L 2.2 of 2.6 s), not fetch.
- LESSON: a silent fallback hid a broken fast path. In v1 the shallow bare made `clone --local --shared` fall back to a non-local clone that saw no refs; APM then silently re-fetched through the downloader's own depth-1 path. Errors stayed 0 while spawns doubled. Always read spawns/connects by caller (profile) before quoting a fetch-shape number.

## #5351 Windows world-cell "regression" = test-order leak (for F&C)
- Not a product regression: it also fails at f251f785 in the world -> windows_world order [45393987117]. windows_world alone passes [45394767505], and so does windows_world first [45394795931].
- Pinned: git_env.reset_git_cache() at fixture setup -> green [45395612990]; forget_invocation_memos() -> still red [45395641798].
- Leaked state: _real_git_tls_config_cache (world A's ambient TLS config re-injected by core/auth.py:1758).

(P3 addendum: W v2 real 12.88 -> 3,423.8 s CPU p50, paired 261 (wall 8.2 -> 1,006 s) [45394378611]. Partial + depth-1 lazy blob fetch is catastrophic on Windows.)

## P4 GIT-PATH CHECKOUT + VERIFY FROM THE PACK (population: authenticated token-first = git CLI path; tree-50 offline auth journey, harness perf-research/arch-sol-attr-auth)
W3d: 2a = SoL P1-P7; lever C = OK-with-conditions C1-C12 (row w3d:ruling:lever-c).
- C6: keep `clone --local --shared --no-checkout`, detached HEAD at the pin; list every .git reader.
- C3+/C5+: links and gitlinks -> git; the commit's .gitattributes refuses; exec bit only from 100755.
- C8-C12 as ruled.
Base auth spawn mix at 4964581e (#5330 + main dbfa810f, #5341 in), t50:
- L [45398186954]: clone 100 (1.99 s), ls-remote 50 (1.36), ls-tree 100 (0.15), checkout 50 (0.14), cat-file 50 (0.07).
- W [45398215302]: clone 3.30, ls-tree 2.63, checkout 1.41, ls-remote 0.78, cat-file 0.69. Children 17.6 s.
Prototype:
- src/apm_cli/cache/pack_objects.py (new): idx v2 locator + pack reader. Every object is re-hashed by the caller.
  - It refuses to: SHA-256, a .git file, nested/http alternates, MIDX, promisor, replace refs/grafts, 64-bit offsets, out-of-pack REF_DELTA, delta depth > 50, caps.
- 2a: checkout_verify._verify tries the pack source, all-or-nothing per package, else cat-file.
- C: git_cache._create_checkout runs clone --no-checkout, then materialize_tree.
  - The gate is B1 #5351's W4 attributes reading plus a filter.*/core.attributesfile refusal. Commit -> tree walk, with every object re-hashed; B1 _safe_name, _plan caps and _write_file; detached HEAD at the pin.
  - It registers the B1 manifest, so verify and links are one-shot; this deletes cat-file and the ls-tree link plan for the fresh landing.
  - Sparse/promisor paths stay on git.
- v1 f5049147 on #5330 (4964581e):
  - L t50 0.961 (spawns 367 -> 267) [45399430953].
  - W 1.017, did NOT engage: #5330's attribute gate refuses Git for Windows' system attributes, and +52 `git var` / +50 config probes came from my per-package gate [45399465623].
- v2 419ee10a on #5351 head 33ec1c16 (W4 gate):
  - Engagement: spawns 365 -> 165 on L and W (checkout 50, ls-tree 100, cat-file 50 -> 0) [profiles W 45400699120, L 45400752559].
  - | OS | t10 | t50 (CPU p50 s) | run |
    |---|---|---|---|
    | Linux | 0.985 | 15.73 -> 14.99, 0.973 | 45400799575 |
    | Windows | 0.881 | 27.56 -> 23.47, 0.862 (13.8%) | 45400831263 |
- Gaps vs W3d (prototype, not product):
  - No .git index is written: `git ls-files`/status readers would see none. The C6 reader list and same-answer cells are still owed.
  - The C8 parent-component proof, C11 differential and tamper cells are not built.
  - Real-history fixture: the authenticated harness token is INVALID ("Invalid username or token", all 6 runs errored [45399550619]). Proxy = real fixture with APM_NO_INPROCESS_FETCH=1 (harness perf-research/arch-bench-gitpath).
- Real fixture, git-path proxy (APM_NO_INPROCESS_FETCH=1, anonymous; harness arch-bench-gitpath):
  - L t50-equivalent real 6.27 -> 6.16 s, 0.993 [45401219674]; W 15.62 -> 15.16 s, 0.974 [45401256127].
  - Spawns 83 -> 80: C engages only for the one whole-repo dep. The other 9 are subdir deps on the partial (blob:none, promisor) bare, which C4 sends to git: their blobs are not local, and `git checkout` lazy-fetches them.
- Readout:
  - Lever C covers whole-repo deps on the git path: W -13.8%, L -2.7% at t50.
  - It does NOT cover monorepo-subdir deps; their checkout cost is network lazy-fetch.
  - Reaching subdirs needs an in-process sparse fetch of the cone's blobs (B1-style want of exactly those blobs). That is a different lever with W3d C10 + B1 conditions.

## P5 SPARSE-BLOB lever (monorepo SUBDIR deps on the git path)
Status: SIZED ON PAPER from a CI probe; not measured on the bench.
- Probe: perf-research/arch-probe-sparse, tests/perf_probe/test_sparse_shape.py; real repos at the real fixture's pins, Linux x3 [45407084343].
- The W run errored: `resource` is missing on Windows. Wall/bytes are OS-independent; W child CPU was not measured.
- W3d ruling: w3d:ruling:lever-s = OK with S1-S12:
  - S1: filter+shallow advertised, else git;
  - S3: request 2 refuses any unrequested or missing blob; caps;
  - S4: B1/W gate or A1-A10 through _admitted_post;
  - S5: #4970 first;
  - S6: C's writer;
  - S7: B1 .git + shallow, NO promisor, reader list;
  - S8: sparse-checkout file byte-equal, index only if a reader needs it;
  - S10: refusal -> rmtree -> git;
  - S12: net fewer requests.
- TODAY, per repo (shared by its subdir deps): `clone --bare --no-tags --filter=blob:none` = all branches' commits + trees.
  - github/awesome-copilot: 5.36 MB, 21,685 objects, ~0.30 s child CPU, ~0.37 s wall.
  - microsoft/apm: 22.5 MB, 113,973 objects, ~2.1 s child CPU (1.7-2.7), ~1.5 s wall.
  - Then per dep: sparse checkout with 1 lazy-fetch child, 85-827 KB, ~0.07-0.15 s child CPU.
- CEILING (lever S shape, measured via git CLI here):
  - Pin trees at depth 1: awesome-copilot 247 KB / 1,211 objects; microsoft/apm 187 KB / 590 objects; ~0.05 s.
  - Then exactly the cone's blobs in ONE request: 1.6 KB (1 blob) to 742 KB (229 blobs); ~0.03-0.06 s.
- Real-fixture subdir set: bytes ~29 MB -> ~1.5 MB (~20x). Git child CPU ~3.3 s -> ~0 (replaced by in-process parse of ~1.8k trees + cone blobs, est. 0.1-0.2 s). Paper ceiling ~40% of real-fixture L cold CPU (6.3-7.2 s); more on W, where children are costlier.
- Not measured: building S faithfully (B1 transport + a 2-request want list + C writer + sparse file/.git shape) is ~half a day. The authenticated real-repo bench is blocked by the invalid APM_BENCH_GITHUB_TOKEN.
- VERDICT per the lead's rule: KILLED FOR TODAY (no measured faithful ceiling by 16:15). Recommended as the top next bet; the physics says >> 10%.
- 15:11 CET VP: lever S = TOP v0.39.1+ bet for monorepo subdir deps, OWNED by PERF-BETS. First deliverable = a MEASURED faithful ceiling on the real-repo fixtures; no build before that number. Gated on the APM_BENCH_GITHUB_TOKEN rotation (escalated to the regent).

## P6 LEVER S, MEASURED FAITHFUL CEILING (population: public github.com anonymous; real fixture = real github.com)
Branch perf-research/arch-lever-s @ 851890b1 (cells fix ababe204), on #5351 head 33ec1c16.
W3d rulings: lever-s S1-S12; lever-s6prime T1-T7; lever-s-t1prime U1-U7.
What it does for a subdir dep:
- #4970 admission first.
- Caps GET once per origin.
- POST 1: pin + all trees (depth 1, blob:none).
- POST 2: exactly the cone's blob ids; the response must equal the wanted set.
- The cone is byte-equal to `sparse-checkout --cone` (cell).
- In-cone .gitattributes are evaluated per written file (checkout_verify engine + case-fold).
- B1 writer; .git without promisor; one-shot verify skip; any refusal -> git.
Engagement probe (real github.com, DEBUG): 9/9 subdir deps "sparse hit" on L and W [45434212506 / 45434272826].
RESULTS (6 pairs, paired CPU incl. children, real fixture, cold):
- Linux: 4.74 -> 2.30 s, paired 0.477 (-52%), spawns 77 -> 11, connects 16 -> 9 [45434310268]. Git children 0.015 s [profile 45434409737].
- Earlier head f81616a4 (microsoft/apm only, T1 strict): L 0.572 [45426702044].
- Windows: 1.009, spawns 83 -> 83 [45434341295]. Not engaging in the bench's isolated real world on W; the whole-repo B1 dep shows no drop there either. Suspect the B1/#5351 gate under the bench env on W. Root cause pending.
Owed for landing:
- U6: a >= 300-case `git check-attr` differential.
- A real-repo install differential cell.
- A' for the authenticated population.
VERDICT: Linux -52% clears the 20% bar -> WIN. Windows engagement is open.
