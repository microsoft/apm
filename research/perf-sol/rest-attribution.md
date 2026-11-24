# "rest" bucket attribution on the v0.39.0 stack e62f520f (PERF-SOL, CI profiles)

Source: the per-thread CPU profiles from the v039 table: L 45446802295, W (bench TLS world) 45446854372.
Owner = the innermost apm_cli frame of each sample; tool files/tools/rest_owners.py.
"rest" = samples whose owner matches none of the named families, plus process CPU not covered by samples.

| group (seconds) | L anon | L auth | W anon | W auth | top owners |
|---|---|---|---|---|---|
| total rest | 1.12 | 1.25 | 2.77 | 2.61 | |
| imports (module bodies) | 0.33 | 0.33 | 0.50 | 0.53 | marketplace.builder, utils.console, security/__init__ (F15: lazy = KILL) |
| config/manifest reads | 0.10 | 0.17 | **0.68** | **0.56** | **config.get_config_if_exists 0.03/0.06/0.36/0.15**, **scope.is_user_scope_root -/0.01/0.15/0.18**, git_config_memo._file_id, manifest_io.read_manifest_text |
| no apm frame / unsampled | 0.22 | 0.28 | 0.36 | 0.37 | interpreter start/teardown, thread pool, GC |
| package validation | 0.15 | 0.12 | 0.31 | 0.40 | models/validation._validate_skill_bundle (iterdir/stat), validate_apm_package, primitive_classification.classify_agent_source_file (open), deployable_source_plan.create |
| cache bookkeeping | 0.07 | 0.10 | 0.26 | 0.18 | git_cache._inprocess_checkout (filelock + mkdir), cache_pin._replace_marker, remember_resolved_ref |
| security content scan | 0.02 | 0.02 | 0.20 | 0.19 | content_scanner.scan_file (open + read_text: re-reads deployed files) |
| resolver/graph glue | 0.08 | 0.07 | 0.20 | 0.15 | github_downloader.download_package, lockfile_write_authority._scope, git_auth_env.isolated_global_config_path |
| cli/version/pipeline glue | 0.06 | 0.05 | 0.10 | 0.08 | version.get_version (importlib.metadata mtime/stat) |
| other | 0.07 | 0.06 | 0.13 | 0.11 | reflink._device_for, git_env.run_git_text |
| lockfile/deploy records | 0.01 | 0.06 | 0.03 | 0.03 | lockfile_deploy_records._file_mode |

Findings:
1. W config.get_config_if_exists (config.py:87-96) caches only a PRESENT config. With no ~/.apm/config.json
   (fresh users and the bench), every experimental-flag read (core/experimental.py:170, docstring :183 claims "no I/O")
   pays os.path.isfile: 0.36 s W anon, 0.15 s W auth, 0.03-0.06 s L. Exact fix: remember "missing" too; the writers
   (update_config, _unset_config_key) already invalidate before and after writing.
2. scope.is_user_scope_root (scope.py:223-232) resolves BOTH sides on every call (ledger codec per lockfile read/write,
   lifecycle scripts); the APM home side is constant: 0.15-0.18 s W. Fix: memo the resolved home keyed by its text.
3. Content scan re-reads every deployed file (0.2 s W); candidate to fold into the hash read (one read per file).
4. get_version reads importlib.metadata (stat + mtime) on each run: 0.03-0.07 s.
Prototype for 1+2: perf-research/arch-rest-cut e2cf95e4 on e62f520f; paired benches below.

## Rest-cut measured (e62f520f -> e2cf95e4, t50, 6 pairs, rtt 30): PARK (< 3% W bar)
| cell | anon paired | auth paired | run |
|---|---|---|---|
| L | 0.999 | 0.997 | 45457830059 |
| W bench world (OpenSSL + CA) | 0.995 | 1.005 | 45457855964 |
| W default world (JH harness 0b673930, schannel) | 0.994 | 0.994 | 45458015183 |
The profile's 0.36 s on get_config_if_exists (W) did not show up as paired CPU: per-call self time of small,
frequently-called functions is inflated by the W sampler (15.6 ms timer granularity, ~1 sample per
call site). Lesson: on W, treat profile buckets under ~0.3 s as candidates only, never as sizing.
Side note: in the default world the stack spawns 168 git processes (vs 18 in the bench world), and t50 is 22.9 s.
B1/A' does not engage under schannel at e62f520f (danielmeppiel-b1-schannel is in flight).
The two defects stay true and are exact to fix: config.py:87-96 negative cache; scope.py:223-232 home memo.
They are hygiene for E2's backlog list, not a perf lever.
