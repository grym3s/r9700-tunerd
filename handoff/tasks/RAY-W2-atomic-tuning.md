# TASK RAY-W2: atomic tuning transaction and versioned status JSON

Base: the reviewed/merged Wave 1 commit (state the exact commit before work).

## Role and scope

You are Ray, the builder, not the approver. Change only:

- `r9700-tunerd`
- `tests/test_unit.py`

Do not touch the UI server/frontend, benchmark/matrix/app tools, docs, config sample,
units, install scripts, or hardware tests. No root, live hardware, service, benchmark,
reboot, or external-network commands.

## Objective

Provide one safe daemon transaction for applying offset plus cap and a stable JSON
status contract for future UI/diagnostic clients.

## Required contract

1. Add `set-tuning --offset-mv N --cap-w W`; both flags are mandatory integers.
2. Discover the target once and obtain one coherent range snapshot before mutation:
   when active, read OD and cap ranges only after the PM guard; when suspended, use
   one validated cached-range object. Refuse missing, malformed, ambiguous, or
   out-of-range evidence.
3. Validate both requested values fully before changing config or hardware. Never
   mutate the first value while discovering that the second is invalid.
4. Add a multi-key config helper that preserves comments/unrelated keys and writes
   both values with one `_atomic_write` replacement. Preserve original bytes/mode for
   rollback.
5. After config commit, call one explicit apply cycle, not two existing setters.
6. On any apply failure or non-success result, restore the exact previous config
   atomically and make one bounded best-effort attempt to re-apply the previous
   validated tuning if the GPU is still active and PM-safe. Return nonzero even if
   rollback succeeds. Log whether config rollback and hardware rollback succeeded;
   never claim atomic success if hardware state is uncertain.
7. Suspended success updates config atomically and reports deferred apply; it must not
   wake or touch OD/hwmon. The watcher applies on the next natural wake.
8. Add `status --json`. Existing `status` human output remains byte-compatible where
   practical. JSON schema version `1` includes: exact identity values, discovered PCI
   path as informational only, runtime status/power state/control, configured offset
   and cap, live readings/ranges only when active, cached ranges/age when asleep,
   service-independent state, and structured errors. Use JSON null for unavailable
   facts; never guess.
9. `status --json` while suspended reads only the existing PM-safe files plus cached
   files/config; never fan/hwmon/OD/DRM.
10. Fix `cmd_reset`: attempt both reset components, but return nonzero when either
    fails or readback does not prove the reset. Preserve refusal behavior when PM is
    unsafe. A suspended reset remains a documented no-op only if current contract
    explicitly retains it; test the chosen behavior.
11. Preserve existing CLI behavior and all runtime-PM invariants. No fan code.

## Acceptance criteria

- Invalid cap with valid offset causes no config/hardware calls.
- Invalid offset with valid cap causes no config/hardware calls.
- Active success performs one atomic config replacement and one apply cycle.
- Suspended/cache success performs no active-only reads and no hardware write.
- Injected config write failure leaves original bytes unchanged.
- Injected apply failure restores exact original config; rollback result is logged and
  command returns nonzero.
- Human status tests remain passing; JSON active and suspended payloads round-trip.
- JSON suspended test asserts no calls to OD, cap, fan, hwmon, or DRM helpers.
- Reset cap-only failure, OD-only failure, both failure, and readback mismatch return
  nonzero.
- Full fake-sysfs unit suite passes.

## Authorized verification

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_unit.py
python3 -m py_compile r9700-tunerd
```

Edit the isolated worktree directly, inspect the diff, and leave changes uncommitted
for independent Halo review. Final response: actual changed files, test evidence,
remaining uncertainty, and request review.
