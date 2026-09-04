## Review: r9700-app.py + install-app.sh

### 1. GPU safety — no finding

`WEBKIT_DISABLE_COMPOSITING_MODE` / `WEBKIT_DISABLE_DMABUF_RENDERER` are set before `gi` import (L19-20), inherited by all WebKit child processes. `HardwareAccelerationPolicy.NEVER` (L161-162) suppresses the GPU process. No `/dev/dri` open path remains. Sound.

### 2. Child-process leak on `__init__` exception — **MEDIUM**

**r9700-app.py:137–168.** `_spawn_ui` (L137) stores the child in `self._child`, but `atexit.register(self._cleanup)` is not called until L168. Any exception between those lines — `Gtk.Window()` (L142), `WebKit2.WebView()` (L156), `Gdk.Screen.get_default()` returning None (L152) — propagates out of `__init__`, Python exits, `atexit` fires but `_cleanup` was never registered. The UI server child is orphaned, holding 127.0.0.1:7970 indefinitely.

*Scenario:* `WEBKIT_DISABLE_COMPOSITING_MODE` is set, but the system has no `libwebkit2gtk-4.1-0` installed. `gi.require_version("WebKit2","4.1")` succeeds (typelib present) but `WebKit2.WebView()` raises `GLib.Error`. The spawned `r9700-ui.py` keeps running.

```diff
--- a/tools/r9700-app.py
+++ b/tools/r9700-app.py
@@ -134,6 +134,10 @@ class R9700App:
         if _port_open(port):
             self._token = _token_from_journal()
         elif not no_spawn:
             self._token, self._child = _spawn_ui(port)
+        # Register cleanup BEFORE any GTK/WebKit call that can raise,
+        # so an orphaned UI server is killed on every exit path.
+        if self._child is not None:
+            atexit.register(self._cleanup)
         # else: --no-spawn and port closed → token stays None → error page
 
         # ── window ─────────────────────────────────────────────────────
@@ -165,7 +169,6 @@ class R9700App:
         self.win.add(self.web)
 
         # ── cleanup on every exit path ─────────────────────────────────
-        atexit.register(self._cleanup)
         signal.signal(signal.SIGINT, lambda *_: self.quit())
         signal.signal(signal.SIGTERM, lambda *_: self.quit())
```

### 3. Token exposure — no finding

Token is read from a pipe (not argv/env). UI server stderr → `DEVNULL` (L73). Window title is static (L142). Error HTML contains no token (L105-118). `log_request` in the UI server strips the query string (r9700-ui.py L424-432). Journal read takes the last `TOKEN:` line, which is the current one. Sound.

### 4. Navigation policy — **LOW**

**r9700-app.py:238-239.** Non-`NAVIGATION_ACTION` decisions (i.e. `NEW_WINDOW_ACTION` from `window.open()` / `target="_blank"`) hit `dec.use()`, permitting a new WebKit window with **no** `decide-policy` handler. That window can navigate to any origin.

*Scenario:* A future UI update adds `<a target="_blank" href="http://192.168.1.1/admin">`. The new window opens unmonitored.

Practical risk is minimal (content is local, user-controlled), but the policy should be deny-by-default:

```diff
--- a/tools/r9700-app.py
+++ b/tools/r9700-app.py
@@ -236,7 +236,8 @@ class R9700App:
                         dec: WebKit2.PolicyDecision,
                         dtype: WebKit2.PolicyDecisionType) -> None:
         if dtype != WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
-            dec.use()
+            dec.ignore()
             return
```

### 5. Installer — no finding

Idempotent (`mkdir -p`, `cat >`, `cp -f`). `--uninstall` removes all three targets. `REPO_ROOT` is absolute via `BASH_SOURCE`. All paths under `$HOME`. `set -euo pipefail` aborts on missing sources. Sound.

---

**Verdict: REQUEST CHANGES** — the `__init__`-exception child leak (Finding 2) is a real resource/port leak on a plausible failure path; the one-line `atexit` move fixes it. The `NEW_WINDOW_ACTION` hole is low but trivially closed.