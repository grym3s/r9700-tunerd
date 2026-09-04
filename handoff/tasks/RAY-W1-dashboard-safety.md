# TASK RAY-W1: dashboard safety and correctness foundation

Base: `/home/grymes/src/r9700-tunerd` `main` at `c1bbf27`.

## Role

You are Ray, the builder. Do not approve your own work. Produce a small, reviewable
patch and unit tests. Do not run hardware, root, benchmark, service, or external
network commands.

## Objective

Harden the local dashboard backend so it is safe to expose as a public GitHub
project. Fix concrete correctness/security defects before custom profiles and matrix
views are added.

## Files you may change

- `tools/r9700-ui.py`
- `tests/test_ui_server.py`
- `tools/r9700-bench.py` only if needed for the shared hwmon-selection correction
- a new `tests/test_bench.py` only if `r9700-bench.py` changes

Do not change `r9700-tunerd`, `ui/index.html`, app-wrapper files, docs, units, config,
or hardware tests. Halo owns the native-wrapper task in parallel.

## Required contract

1. Every `/api/*` GET and POST route, including `/api/bench`, must reject a missing
   or wrong token with 403 before reading data or starting work. Keep `/` and static
   UI loading unchanged.
2. `_api_set` must require a JSON object and validate the complete request before
   invoking any subprocess. A request such as `{offset_mv:-50, cap_w:10}` must make
   zero CLI calls. Reject booleans as integers. Reject unknown fields. Require at
   least one of `offset_mv` or `cap_w`.
3. Serialize mutating API operations (`set`, `apply`, `reset`, `bench/run`) so two
   requests cannot race. A nonblocking lock returning 409 is preferred. Do not hold
   the lock after the synchronous operation finishes; the existing asynchronous
   benchmark ownership remains under its benchmark lock.
4. Refresh config-derived `tuned` values on every status build (or after each
   mutation) so a successful Apply is visible without restarting the UI server.
   Never read sensors just to refresh config.
5. Bound and sanitize benchmark launch input: label must be 1..64 characters and
   filename-safe (`A-Z a-z 0-9 . _ -`, no path separators/control characters),
   `prompts` remains integer 1..6 excluding bool, and `max_tokens` must be an integer
   excluding bool in 1..4096.
6. `_list_bench` must return `errors` as an integer count, tolerating legacy files
   where errors is a list, number, null, or malformed value. A result is `stable`
   only when both stability fields are explicitly true, error count is zero, and
   D3cold recovery is present and nonnegative. Do not silently mark missing fields
   successful.
7. Correct `GpuSysfs._find_hwmon`: numeric `hwmonN` ordering, prefer a candidate
   containing `power1_cap`, and ignore malformed names. Re-resolve once if the cached
   hwmon path disappears after a driver rebind. If you also correct the benchmark
   helper, use the same behavior and tests.
8. PCI discovery must collect exact identity matches and refuse ambiguity rather
   than silently selecting the first match. Preserve identity by vendor/device/
   subsystem; never encode card numbers or bus addresses.
9. Preserve runtime-PM safety: while suspended, only the already-approved PM files
   may be read. Never open `/dev/dri`; never add polling, GPU wakeups, shell=True, or
   new network access.
10. Do not attempt the future atomic daemon change here. This task prevents schema-
    invalid partial mutation; a later daemon `set-tuning` task will provide true
    transactional config/apply semantics.

## Acceptance criteria

- All `/api/*` GET routes in the current server return 403 without a token; all POST
  routes already do and stay covered.
- `{offset_mv:-50, cap_w:10}`, a non-object body, unknown keys, and boolean numeric
  values cause zero mocked subprocess calls.
- Two simultaneous mutation requests cannot execute concurrently; the loser gets
  409 and performs no subprocess call.
- A mocked successful setting change is reflected by the next `/api/status` response
  without server restart.
- Unsafe benchmark labels and excessive/boolean numeric options are rejected.
- Missing stability/error/D3cold evidence can never be reported as stable.
- With stale `hwmon7` lacking `power1_cap` and valid `hwmon12`, `hwmon12` is chosen.
- Multiple exact PCI identity matches result in no selected GPU and no hardware read.
- Existing tests plus all new tests pass.

## Authorized verification

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_ui_server.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_bench.py  # only if created
python3 -m py_compile tools/r9700-ui.py tools/r9700-bench.py
```

## Output

Return one unified git diff against the stated base, then a concise changed-file
list, test commands/results, unresolved uncertainties, and an explicit request for
independent review. Do not claim approval. Keep the patch complete; do not use
ellipsis or omit unchanged context required by `git apply`.
