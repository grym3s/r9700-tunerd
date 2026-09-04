#!/usr/bin/env python3
"""r9700-app.py — GTK 3 / WebKit2GTK 4.1 native app wrapper for the R9700 dashboard.

Hosts the local UI server (tools/r9700-ui.py) inside a WebKit web view.

GPU-safety: the wrapper must NOT create GPU contexts or DRM handles.
Hardware acceleration is forced OFF (WebKit policy NEVER + env vars below)
so the app cannot open /dev/dri or allocate DMA-BUFs, preserving the
daemon rule "never open /dev/dri" for the card under test.

Stdlib + PyGObject only.  No network beyond 127.0.0.1.
"""
from __future__ import annotations

import os
# Disable WebKit GPU paths BEFORE any gi import.
#   COMPOSITING_MODE  → no GL compositing thread
#   DMABUF_RENDERER   → no DMA-BUF / DRM handle allocation
os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"
os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"

import argparse
import atexit
import json
import socket
import subprocess
import html
import urllib.parse
from pathlib import Path
from typing import Optional

from r9700_app_backend import Backend, server_is_dashboard

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib  # noqa: E402
# Wayland app_id / X11 WM_CLASS come from the program name; set it before
# Gtk initialises so Hyprland rules and the .desktop StartupWMClass match.
GLib.set_prgname("r9700-tuner")
from gi.repository import Gtk, Gdk, WebKit2, Gio  # noqa: E402

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
UI_SCRIPT: Path = REPO_ROOT / "tools" / "r9700-ui.py"
WIN_STATE: Path = Path.home() / ".config" / "r9700-tuner" / "window.json"
ALLOWED_HOST: str = "127.0.0.1"


# ─── helpers ─────────────────────────────────────────────────────────────────

def _port_open(port: int) -> bool:
    """True if something accepts TCP on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _token_from_journal() -> Optional[str]:
    """Newest TOKEN line from the user-unit journal (or None)."""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "r9700-ui.service",
             "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token: Optional[str] = None
    for line in r.stdout.splitlines():
        if line.startswith("TOKEN: "):
            token = line[7:].strip()
    return token


def _r9700_drm_nodes() -> set[str]:
    """/dev/dri paths belonging to the R9700 (vendor 0x1002, device 0x7551)."""
    nodes: set[str] = set()
    for n in Path("/sys/class/drm").glob("*"):
        dev = n / "device"
        try:
            vendor = (dev / "vendor").read_text().strip()
            device = (dev / "device").read_text().strip()
        except OSError:
            continue
        if vendor == "0x1002" and device == "0x7551":
            nodes.add(f"/dev/dri/{n.name}")
    return nodes


def _err_html(msg: str) -> str:
    """Plain in-window error page with a Retry link (no dialogs)."""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        'body{background:#1a1a2e;color:#ccc;font-family:monospace;'
        'display:flex;align-items:center;justify-content:center;'
        'height:100vh;margin:0}'
        'div{text-align:center;max-width:500px;padding:2em}'
        'a{color:#4fc3f7;font-size:1.1em;margin-top:1em;'
        'text-decoration:none;display:inline-block}'
        '</style></head><body><div><h2>R9700 Tuner</h2><p>'
        + html.escape(msg, quote=True) +
        '</p><a href="r9700app://retry">Retry</a></div></body></html>'
    )


# ─── main app ────────────────────────────────────────────────────────────────

class R9700App:
    """GTK 3 window hosting a WebKit2 web view for the R9700 dashboard."""

    def __init__(self, port: int, devtools: bool, no_spawn: bool) -> None:
        self.port = port
        self._token: Optional[str] = None
        self._child: Optional[subprocess.Popen] = None
        self._fullscreen = False
        self._cleaned = False
        self._backend = Backend(port, REPO_ROOT, _token_from_journal)
        # Register before resolution or any GTK/WebKit call: startup failures
        # must not orphan a server child.
        atexit.register(self._cleanup)

        # ── resolve token ──────────────────────────────────────────────
        self._token = self._backend.resolve(no_spawn)
        self._child = self._backend.child
        # else: --no-spawn and port closed → token stays None → error page

        # ── window ─────────────────────────────────────────────────────
        self.win = Gtk.Window(title="R9700 Tuner")
        self._restore_size()
        self.win.connect("delete-event", lambda *_: self.quit())
        self.win.connect("key-press-event", self._on_key)

        # dark background → no white flash before the page loads
        prov = Gtk.CssProvider()
        prov.load_from_data(b"window{background-color:#1a1a2e}")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # ── web view ───────────────────────────────────────────────────
        self.web = WebKit2.WebView()
        st = self.web.get_settings()
        st.set_property("enable-javascript", True)
        st.set_property("enable-developer-extras", devtools)
        # NEVER: no GPU contexts, no DRM handles (see module docstring).
        # The policy lives on WebKitSettings, not on the view.
        st.set_hardware_acceleration_policy(
            WebKit2.HardwareAccelerationPolicy.NEVER)
        self.web.connect("decide-policy", self._decide_policy)
        self.web.connect("load-failed", self._on_load_failed)
        self.win.add(self.web)

        # ── cleanup on every exit path ─────────────────────────────────
        # Deliberately NO SIGTERM/SIGINT handlers. Two instances were observed
        # with the GTK main thread stuck in a futex wait; a GLib signal
        # handler never runs in that state, so the window ignored SIGTERM and
        # systemd/Hyprland had to SIGKILL it. With the default disposition the
        # kernel terminates us immediately, and a spawned server child is
        # torn down by PR_SET_PDEATHSIG (see _spawn_ui). Window close and
        # Ctrl+Q still go through quit() for the graceful path.

        self.win.show_all()
        # Attached to the user unit (not our own child): the unit's token
        # rotates on every restart, which would strand this window with 403s.
        # Poll the journal and reload when the token changes or the server
        # goes away.
        if self._child is None:
            GLib.timeout_add_seconds(10, self._refresh_attached_token)
        else:
            GLib.timeout_add_seconds(1, self._watch_child)
        # Prove the GPU-safety claim at runtime: after GTK/WebKit are up,
        # none of our file descriptors may point at the R9700's DRM nodes.
        GLib.timeout_add(1500, self._check_no_r9700_drm)

        # ── initial load ───────────────────────────────────────────────
        if self._token:
            self.web.load_uri(
                f"http://{ALLOWED_HOST}:{self.port}/?t={self._token}")
        else:
            self.web.load_html(_err_html(
                "Could not obtain a session token.<br>"
                "If the UI server runs as a user unit, restart it:<br>"
                "<code>systemctl --user restart r9700-ui.service</code>"
            ), None)

    # ── GPU-safety self-check ──────────────────────────────────────────

    def _check_no_r9700_drm(self) -> bool:
        """Refuse to run if this process holds a DRM handle on the R9700.

        The display GPU's nodes (the Strix iGPU) are legitimately opened by
        the Wayland/EGL stack; the card under test must never be.  Identity is
        vendor/device (0x1002:0x7551), never a card number.
        """
        bad = _r9700_drm_nodes()
        held = set()
        try:
            for fd in Path("/proc/self/fd").iterdir():
                try:
                    tgt = os.readlink(fd)
                except OSError:
                    continue
                if tgt in bad:
                    held.add(tgt)
        except OSError:
            return GLib.SOURCE_REMOVE
        if held:
            self.web.load_html(_err_html(
                "Refusing to run: this process opened the R9700's DRM node "
                f"({', '.join(sorted(held))}). That would hold the card out "
                "of D3cold. Check WEBKIT_DISABLE_* and the acceleration policy."),
                None)
        return GLib.SOURCE_REMOVE

    def _refresh_attached_token(self) -> bool:
        """Follow token rotation / restarts of r9700-ui.service (every 10 s)."""
        if not _port_open(self.port):
            if self._token is not None:
                self._token = None
                self.web.load_html(_err_html(
                    "The dashboard server is not running.<br>"
                    "<code>systemctl --user status r9700-ui.service</code>"), None)
            return GLib.SOURCE_CONTINUE
        tok = _token_from_journal()
        if tok and tok != self._token and server_is_dashboard(self.port, tok):
            self._token = tok
            self.web.load_uri(f"http://{ALLOWED_HOST}:{self.port}/?t={tok}")
        return GLib.SOURCE_CONTINUE

    def _watch_child(self) -> bool:
        if self._backend.child_stopped():
            self._backend.fail_child()
            self._child = None
            self._token = None
            self.web.load_html(_err_html("The dashboard server stopped."), None)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _retry(self) -> None:
        self._backend.fail_child()
        self._token = self._backend.resolve(False)
        self._child = self._backend.child
        if self._token:
            self.web.load_uri(f"http://{ALLOWED_HOST}:{self.port}/?t={self._token}")
            GLib.timeout_add_seconds(1, self._watch_child)
        else:
            self.web.load_html(_err_html("Could not connect to the dashboard server."), None)

    # ── window-size persistence ────────────────────────────────────────

    def _restore_size(self) -> None:
        try:
            d = json.loads(WIN_STATE.read_text())
            self.win.set_default_size(int(d["w"]), int(d["h"]))
        except Exception:
            self.win.set_default_size(1280, 860)

    def _save_size(self) -> None:
        try:
            WIN_STATE.parent.mkdir(parents=True, exist_ok=True)
            w, h = self.win.get_size()
            WIN_STATE.write_text(json.dumps({"w": w, "h": h}))
        except Exception:
            pass  # never let size persistence block shutdown

    # ── lifecycle ──────────────────────────────────────────────────────

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._save_size()
        self._backend.cleanup()
        self._child = None

    def quit(self) -> None:
        self._cleanup()
        Gtk.main_quit()

    # ── keyboard ───────────────────────────────────────────────────────

    def _on_key(self, _w: Gtk.Window, ev: Gdk.EventKey) -> bool:
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and ev.keyval == Gdk.KEY_r:
            self.web.reload()
            return True
        if ctrl and ev.keyval == Gdk.KEY_q:
            self.quit()
            return True
        if ev.keyval == Gdk.KEY_F11:
            if self._fullscreen:
                self.win.unfullscreen()
            else:
                self.win.fullscreen()
            self._fullscreen = not self._fullscreen
            return True
        return False

    # ── navigation policy ──────────────────────────────────────────────

    def _decide_policy(self, wv: WebKit2.WebView,
                       dec: WebKit2.PolicyDecision,
                       dtype: WebKit2.PolicyDecisionType) -> None:
        if dtype == WebKit2.PolicyDecisionType.RESPONSE:
            dec.use()
            return
        # NAVIGATION_ACTION and NEW_WINDOW_ACTION both carry a navigation action
        uri = dec.get_navigation_action().get_request().get_uri()
        if dtype == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            if urllib.parse.urlparse(uri).scheme in ("http", "https"):
                Gio.AppInfo.launch_default_for_uri(uri, None)
            dec.ignore()
            return
        # Retry pseudo-URI → reload the main page
        if uri == "r9700app://retry":
            self._retry()
            dec.ignore()
            return
        # Only our own origin is allowed inside the window
        parsed = urllib.parse.urlparse(uri)
        try:
            local_origin = (parsed.scheme == "http" and
                            parsed.hostname == ALLOWED_HOST and
                            parsed.port == self.port and
                            parsed.path.startswith("/"))
        except ValueError:
            local_origin = False
        if local_origin:
            dec.use()
            return
        # External link → open in default browser, cancel in view
        if parsed.scheme in ("http", "https"):
            Gio.AppInfo.launch_default_for_uri(uri, None)
        dec.ignore()

    # ── load failure ───────────────────────────────────────────────────

    def _on_load_failed(self, wv: WebKit2.WebView, _event: WebKit2.LoadEvent,
                        _uri: str, err: Optional[GLib.Error]) -> bool:
        msg = err.get_message() if err else "unknown error"
        wv.load_html(_err_html(f"Load failed: {msg}"), None)
        return True


# ─── entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(prog="r9700-tuner")
    ap.add_argument("--port", type=int, default=7970)
    ap.add_argument("--devtools", action="store_true")
    ap.add_argument("--no-spawn", action="store_true",
                    help="attach to an existing server only")
    args = ap.parse_args()

    devtools = args.devtools or os.environ.get("R9700_APP_DEVTOOLS") == "1"
    R9700App(args.port, devtools, args.no_spawn)
    Gtk.main()


if __name__ == "__main__":
    main()
