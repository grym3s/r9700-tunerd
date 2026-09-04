# TASK 30 (QWEN-A): native Linux app wrapper for the dashboard

The owner wants the dashboard as a Linux *app*, not a browser tab. Build `tools/r9700-app.py`: a GTK 3 window hosting a WebKit2GTK 4.1 web view (both are installed: `python-gobject`, `webkit2gtk-4.1`; there is NO webkitgtk-6.0, so do not use GTK 4 WebKit). The session is Wayland (Hyprland). Output COMPLETE file contents for every file you create or change. Stdlib + PyGObject only.

## Behaviour
1. **Backend ownership.** On start, the app finds or starts the UI server (`tools/r9700-ui.py`, attached; it prints `TOKEN: <hex>` then the bare URL on stdout at start, binds 127.0.0.1:7970 by default).
   a. If TCP 127.0.0.1:7970 already accepts connections, assume the user unit `r9700-ui.service` is running and read the newest `TOKEN: ` line from `journalctl --user -u r9700-ui.service -o cat --no-pager`. If no token can be read, show an in-window error page explaining that and how to restart the unit; do not crash.
   b. Otherwise spawn `sys.executable tools/r9700-ui.py --port 7970` (cwd = repo root = parent of `tools/`), read stdout lines until the `TOKEN: ` line (timeout 10 s), and terminate that child (SIGTERM, then SIGKILL after 3 s) when the window closes. Do not leak the child on any exit path (also `atexit` and SIGINT/SIGTERM handlers).
   c. Never put the token in argv, environment of other processes, logs, or window title. Pass it only in the URL the WebView loads.
2. **Window.** Title "R9700 Tuner", default 1280×860, remembers size in `~/.config/r9700-tuner/window.json` (ignore any error). Dark background so there is no white flash before the page loads. WebKit settings: enable javascript, disable developer extras unless `R9700_APP_DEVTOOLS=1`, `hardware-acceleration-policy` NEVER (this is a tuning tool for the GPU under test; the wrapper must not create GPU contexts or DRM handles itself — the daemon rule "never open /dev/dri" applies to the app too; explain this in a comment). Set `WEBKIT_DISABLE_COMPOSITING_MODE=1` and `WEBKIT_DISABLE_DMABUF_RENDERER=1` in the app's own environment before importing gi, for the same reason.
3. **Navigation policy.** Only `http://127.0.0.1:7970/` URLs load inside the window; any other link opens with `Gio.AppInfo.launch_default_for_uri` and is cancelled in the view. Refuse to load if the server is reachable but the URL is not 127.0.0.1.
4. **Keyboard.** Ctrl+R reloads, Ctrl+Q quits, F11 toggles fullscreen.
5. **Failure UX.** If the child dies or the page fails to load (`load-failed` signal), show a plain in-window HTML page (`load_html`) with the reason and a Retry button (a link to a `r9700app://retry` URI you intercept in the policy handler). No dialogs.
6. `--port N` and `--devtools` CLI flags. `--no-spawn` to only attach to an existing server.

## Launcher files
- `contrib/r9700-tuner.desktop`: Name=R9700 Tuner, Comment, `Exec=r9700-tuner`, `Icon=r9700-tuner`, Terminal=false, Type=Application, Categories=System;Settings;HardwareSettings;, StartupWMClass=r9700-tuner (also set `Gtk.Window.set_wmclass`/`GLib.set_prgname("r9700-tuner")` so Hyprland matches it).
- `contrib/r9700-tuner.svg`: a simple flat icon (a stylised GPU card silhouette with a small lightning bolt), 64×64 viewBox, two colours, no external references.
- `tools/install-app.sh`: user-level install, NO root: copies/links `r9700-app.py` to `~/.local/bin/r9700-tuner` (a 3-line bash launcher that `exec`s `python3 <repo>/tools/r9700-app.py "$@"` with the absolute repo path baked in at install time), installs the .desktop to `~/.local/share/applications/`, the icon to `~/.local/share/icons/hicolor/scalable/apps/`, runs `update-desktop-database ~/.local/share/applications` if available. Idempotent. `--uninstall` reverses it.

## Docs
Add a "Run as an app" section to `docs/UI.md` (attached) describing install-app.sh, the attach-vs-spawn logic, and why hardware acceleration is off in the wrapper.

## Constraints
- No network beyond 127.0.0.1. No writes outside `~/.config/r9700-tuner/`, `~/.local/bin`, `~/.local/share`.
- Keep `r9700-app.py` under ~250 lines, well commented. Type hints. `python3 -m py_compile` clean.
- Do not modify `tools/r9700-ui.py` or `ui/index.html`.
