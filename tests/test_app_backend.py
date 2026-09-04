from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools import r9700_app_backend as backend


class _Handler(BaseHTTPRequestHandler):
    token = "good"
    shape = {"runtime_status": "suspended", "power_state": "D3cold"}
    def do_GET(self):
        if self.path != "/api/status" or self.headers.get("X-Token") != self.token:
            self.send_response(403); self.end_headers(); return
        raw = json.dumps(self.shape).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
    try:
        yield srv
    finally:
        srv.shutdown(); srv.server_close(); thread.join()


def test_authenticated_dashboard_shape(server):
    assert backend.server_is_dashboard(server.server_port, "good")
    assert not backend.server_is_dashboard(server.server_port, "stale")


def test_unrelated_listener_is_refused(server):
    _Handler.shape = {"hello": "world"}
    try:
        assert not backend.server_is_dashboard(server.server_port, "good")
    finally:
        _Handler.shape = {"runtime_status": "suspended", "power_state": "D3cold"}


def test_cleanup_only_owns_child(monkeypatch, tmp_path: Path):
    b = backend.Backend(1, tmp_path, lambda: None)
    called = []
    monkeypatch.setattr(backend, "terminate_child", lambda p: called.append(p))
    b.cleanup()
    assert called == [None]


def test_parent_setup_uses_captured_pid(monkeypatch):
    seen = []
    monkeypatch.setattr(backend.os, "getppid", lambda: 44)
    monkeypatch.setattr(backend.os, "kill", lambda pid, sig: seen.append((pid, sig)))
    class Libc:
        def prctl(self, *args): return 0
    monkeypatch.setattr(backend.ctypes if hasattr(backend, "ctypes") else __import__("ctypes"), "CDLL", lambda *a, **k: Libc())
    # A normal parent remains valid after prctl; this exercises the race check.
    backend._die_with_parent(44)
    assert seen and seen[0][0] == 44
