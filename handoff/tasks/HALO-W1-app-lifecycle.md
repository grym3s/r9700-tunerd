# TASK HALO-W1: native app lifecycle reliability and testable backend

Base: `/home/grymes/src/r9700-tunerd` `main` at `c1bbf27`.

## Role

You are Halo, the builder for this task, not the reviewer. Implement the lifecycle
hardening with stdlib/PyGObject only and request independent review when done. Do not
run hardware, root, benchmark, service-management, GUI, or external-network tests.

## Objective

Make the GTK/WebKit native wrapper robust enough for public users: distinguish the
real UI server from an unrelated listener, recover from backend death, make Retry
actually attach or spawn again, close parent-death races, and unit-test the lifecycle
without importing GTK.

## Files you may change

- `tools/r9700-app.py`
- preferably add `tools/r9700_app_backend.py` for stdlib-only lifecycle logic
- add `tests/test_app_backend.py`

Do not change `tools/r9700-ui.py`, `ui/index.html`, the daemon, install scripts,
desktop/icon files, docs, units, or hardware tests. Ray owns the UI backend in
parallel.

## Required contract

1. Extract enough attach/spawn/token/child-lifecycle behavior into a stdlib-only
   module that pytest can load without `gi`, a display, WebKit, or a real service.
2. Do not trust “port 7970 accepts TCP” as proof of identity. An attach succeeds only
   when a journal token is found and an authenticated request to the local server
   returns the expected dashboard status shape. A wrong process or stale token must
   show the in-window failure page and must not receive any unsafe request.
3. Bound journal lookup to the current/recent user-unit invocation where practical;
   never log, display, or include the token in error text, argv, environment, title,
   or tests. Continue using constant local origin only.
4. Monitor a spawned child without blocking GTK. When it exits, clear its token,
   show an escaped in-window backend-stopped error, and allow recovery.
5. Retry must perform the full attach-or-spawn decision again. It must not merely
   reload a stale token or dead child. Repeated Retry clicks must not create parallel
   children.
6. Preserve default SIGTERM/SIGINT disposition. Do not restore GLib/Python signal
   handlers; the documented futex hang made handlers unsafe. Keep `PR_SET_PDEATHSIG`.
7. Close the PDEATHSIG race in child setup: capture parent PID, call `prctl`, then
   verify the parent PID is unchanged/alive and terminate immediately if the parent
   died in the gap.
8. Cleanup must be idempotent and bounded: terminate owned child, wait at most 3 s,
   kill if needed. Never terminate an attached user service or an unrelated listener.
9. Escape every dynamic reason inserted into error HTML. External navigation may
   launch only `http` or `https`; ignore unexpected schemes. Continue allowing only
   the exact `http://127.0.0.1:<configured-port>/` origin in WebKit.
10. Preserve token-rotation following, software rendering, the R9700 self-check,
    window persistence, keyboard shortcuts, and no `/dev/dri` opens by app code.

## Acceptance criteria

- Stdlib-only tests cover attach success, stale/wrong listener refusal, stale journal
  token, spawn success, child death, Retry respawn, no duplicate child, token change,
  idempotent cleanup, and PDEATHSIG parent-race behavior.
- Killing a mocked spawned backend transitions the controller to failed and a Retry
  creates exactly one replacement.
- Cleanup never signals a process the app did not spawn.
- Error reasons containing markup render as literal text.
- Non-http(s) external schemes are ignored.
- `python3 -m py_compile` succeeds without needing a display.
- Existing wrapper behavior remains compatible with GTK 3 and WebKit2GTK 4.1.

## Authorized verification

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_app_backend.py
python3 -m py_compile tools/r9700-app.py tools/r9700_app_backend.py
```

No launch soak, D3cold test, service restart, or live GUI run is authorized in this
builder task. Those are independent release gates after review.

## Output

Return one unified git diff against the stated base, then a concise changed-file
list, test commands/results, unresolved uncertainties, and an explicit request for
independent review. Do not claim approval. Keep the patch complete; do not use
ellipsis or omit context required by `git apply`.
