# TASK HALO-W2: reproducible fail-closed experiment runner

Base: the reviewed/merged Wave 1 commit (state the exact commit before work).

## Role and scope

You are Halo, the builder, not the approver. Change only:

- `tools/r9700-matrix.py`
- new `tests/test_matrix.py`

Do not touch the daemon, UI/app tools, existing tests, docs, config, units, install
scripts, or hardware tests. No live daemon, root, GPU, benchmark, service, reboot, or
external-network commands. All tests use mocks/temp files.

## Objective

Make tuning experiments reproducible, resume correctly by repetition, eliminate the
documented thermal-order confound, and fail closed without leaving an experimental
setting active.

## Required contract

1. Add `--order sequential|random` (default sequential for compatibility) and
   `--seed INT`. Random order must be deterministic for a given inputs+seed. Print
   and record the effective seed.
2. Give each plan a stable run ID and each step an explicit repeat index. Extend CSV
   compatibly with `run_id`, `repeat`, `order`, `seed`, `timestamp`, `model`, and
   `gate_reason`; old CSV rows remain readable.
3. `--start-from` resumes missing individual `(offset,cap,repeat)` entries for the
   selected run/plan. One existing row must not skip all requested repeats. Define
   clear behavior for legacy rows without repeat metadata and test it.
4. Add `--cooldown-s N` after D3cold is observed. Default 0 preserves old behavior;
   rigorous/random/repeated runs may require an explicit nonzero value, but state the
   rule clearly.
5. Capture a journal cursor/timestamp before applying the experimental setting so
   apply-time kernel faults are inside the gate window.
6. In rigorous mode (add `--rigorous`, and use it automatically for random order or
   repeats >1), inability to query kernel logs, D3cold timeout, benchmark failure,
   readback instability, kernel fault, or missing result evidence is a hard step
   failure.
7. Record every failed/incomplete step with `verdict=FAIL` and a machine-readable
   `gate_reason` before aborting when it is safe to write the row.
8. Restore the initial config safe point on normal completion, every failure,
   timeout, KeyboardInterrupt, and unexpected exception. Default is restore. Provide
   an explicit `--keep-final` opt-out allowed only after every step passed; it must
   never suppress restoration on failure/interruption.
9. If restoration cannot be verified, emit an unmistakable manual-action message and
   exit nonzero. Never mask the original failure.
10. Dry-run prints exact randomized plan, repeat IDs, cool-downs, restore policy, and
    performs no subprocess or hardware access beyond the existing non-mutating
    planning behavior; improve separability for pure testing if needed.
11. Keep dangerous/benign kernel precedence and all existing range/offset guards.
    Prefer argv lists; never `shell=True`.

## Acceptance criteria

- Same seed/input produces identical plan; different seed changes a multi-step plan.
- Every combination has exactly the requested repeat indexes.
- Resume skips only completed PASS repetitions and reruns failed/missing ones.
- D3cold timeout, journal query failure, apply failure, benchmark failure, missing
  JSON, kernel fault, unstable settings, Ctrl+C, and arbitrary exception all attempt
  restoration and exit nonzero.
- A fault emitted during Apply is included in the kernel gate.
- Normal success restores by default; `--keep-final` works only after all PASS.
- Failed rows remain parseable by old-report compatibility code.
- Tests touch no real `/sys`, config, journal, daemon, endpoint, or GPU.

## Authorized verification

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_matrix.py
python3 -m py_compile tools/r9700-matrix.py
```

Edit the isolated worktree directly, inspect the diff, and leave changes uncommitted
for independent Ray review. Final response: actual changed files, test evidence,
remaining uncertainty, and request review.
