# Windows exact Path.resolve analogue (form B) handoff (for IFP or the assigned builder)

Branch `perf-research/arch-win-paths`, measured head 7af4bc00; the differential head is 4f6cb937. Base is 0cee8358 (#5330 + Windows follow-up + #5333 + main).
- One file changes: `src/apm_cli/utils/realpath.py`, +75 / -0, of which ~10 lines are the differential.
- It stacks on IFP's call-count cut (danielmeppiel-perf-resolve-fewer). There is no file overlap.

## What it changes (file:line at 7af4bc00)
- `realpath.py:80-92`: Windows constants: the `\\?\` prefix, reserved device names (incl. COM/LPT superscripts, CONIN$/CONOUT$), bad characters, and the `~digit` 8.3 pattern.
- `realpath.py:94-110` `_plain_win32`: proves Win32 parses `X:\...` to the same NT path as `\\?\X:\...`. Requires: under 260 chars; drive-letter root; no empty component; no trailing dot or space; no `<>"|?*:/` or control characters; no reserved name (with or without extension); no `~digit`. This is W3d B2.
- `realpath.py:112-128` `_win_realpath`: one `nt._getfinalpathname` call, the same call CPython makes (W3d B1). On success, the `\\?\`-stripped result is returned only when it is plain Win32 (B2), so CPython's second `_getfinalpathname` is proven by construction. Any error or missing leaf returns None (B3).
- `realpath.py:130-148` `_resolve_windows`: stock `WindowsPath` only (a subclass or an overridden resolve keeps its own behavior). With `_WIN_DIFFERENTIAL` it computes `Path.resolve()` too and raises on any difference.
  - Exactness note: it also skips `Path.resolve`'s non-strict loop-check `stat()`. That is exact because a path `_getfinalpathname` opened cannot be a symlink loop.
- `realpath.py:166-169`: `resolve_path` takes the Windows branch after #5333's Linux branch. There is no cache (B4), and it applies only behind `resolve_path` (B5).

## W3d (row w3d:ruling:win-paths) form B conditions
- B1 same open flags as CPython: DONE (calls `nt._getfinalpathname` itself).
- B2 strip only when proven: DONE (`_plain_win32`).
- B3 missing leaf or any error falls back: DONE.
- B4 no cache: DONE.
- B5 behind `resolve_path` only: DONE.
- B6 windows-latest differential cell covering junctions, subst, UNC, 8.3, >260, trailing dot, reserved names: TODO. The bench differential (4f6cb937) covers only the fixture's paths.
- B7 cells: one counting calls, one for a refused strip: TODO.

## Evidence (install-cold, 6 pairs, base 0cee8358)
| population | OS | t10 | t50 | run |
|---|---|---|---|---|
| public github.com anonymous | W | 0.970 | 0.948 (19.79 -> 18.67 s) | 45390633875 |
| authenticated (JH harness, install-cold-auth) | W | - | 0.993 | 45390685826 |
- Differential SHA 4f6cb937: 0 errors on W t10/t50 (45390597410).
- Profile 45390708927, os.path.realpath calls: 38,884 -> 10,153, so 74% take the fast path. Realpath leaf 7.20 -> 2.26 s; stat 4.20 -> 3.37 s.
- Ceiling for context (never ships; per-command memo): W anonymous t50 0.72 (45390109666 / 45390308062). Form B keeps ~21% of it. The rest needs form A (roots once plus held handles, W3d A1-A9) or fewer calls (IFP round 2).
- Linux: the Windows branch is inert (`_WIN_FAST` is False).

## VP gates (13:21 CET, verbatim) and the measured verdict on #5354
"(B) ships only if its INCREMENT over #5354 is W anon cold t50 paired p50 <= 0.97 with L/M <= 1.010, identical requests, differential 0 errors, W3d B1-B5 met; else it parks."
Base 23989789 = #5354 head 046e105a + #5333 head 77f79776 (needed: #5354 is cut from main without utils/realpath.py; one trivial conflict in plugin_root_render.bind_plugin_roots resolved as lazy + resolve_path). (B) = 1b307ec2; differential = 612bc4e3.
| gate | result | run |
|---|---|---|
| W anon cold t50 paired p50 <= 0.97 | 0.974 (44.95 -> 43.52 s); t10 0.992 | 45392847051 |
| L <= 1.010 | t50 1.000, t10 1.003 | 45392890045 |
| M <= 1.010 | t50 0.964, t10 0.858 (inert on macOS: noise) | 45392931465 |
| identical requests | 201 -> 201 on W/L/M | same runs |
| differential 0 errors | 0 errors W t10/t50 | 45392817168 |
| W3d B1-B5 | met | - |
VERDICT: PARKS (W 0.974 > 0.97 by 0.004). Stacked with faithful (A) (F11): W anon 0.996, W auth 0.988 vs gates 0.90/0.97 -> both PARK. #5354's call-count cut removes part of what (B) speeds up (on 0cee8358 without #5354, (B) was 0.948). Re-evaluate stacked with form (A) below.
