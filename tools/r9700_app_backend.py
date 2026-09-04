"""GTK-free lifecycle helpers for the native dashboard wrapper."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional


def port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def server_is_dashboard(port: int, token: str, timeout: float = 2.0) -> bool:
    """Authenticate a local server and require the dashboard status shape."""
    if not token:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/status",
        headers={"X-Token": token}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return False
            import json
            obj = json.loads(response.read(65536))
            return (isinstance(obj, dict) and
                    "runtime_status" in obj and "power_state" in obj)
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return False


def _die_with_parent(parent_pid: int) -> None:
    """Install PDEATHSIG, then close the parent-death setup race."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            os._exit(1)
        if os.getppid() != parent_pid:
            os._exit(1)
        try:
            os.kill(parent_pid, 0)
        except OSError:
            os._exit(1)
    except Exception:
        # On non-Linux platforms, retain the best-effort behavior.
        if os.getppid() != parent_pid:
            os._exit(1)


def terminate_child(proc: Optional[subprocess.Popen], timeout: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Backend:
    """Own at most one spawned child and resolve attach/spawn on each retry."""

    def __init__(self, port: int, repo_root: Path, journal_token: Callable[[], Optional[str]],
                 script: Optional[Path] = None) -> None:
        self.port, self.repo_root = port, repo_root
        self.journal_token = journal_token
        self.script = script or repo_root / "tools" / "r9700-ui.py"
        self.child: Optional[subprocess.Popen] = None
        self.token: Optional[str] = None
        self.owned = False
        self._lock = threading.Lock()

    def resolve(self, no_spawn: bool = False) -> Optional[str]:
        """Attach only after auth; otherwise spawn (unless disabled)."""
        with self._lock:
            self.token = None
            if port_open(self.port):
                candidate = self.journal_token()
                if candidate and server_is_dashboard(self.port, candidate):
                    self.token = candidate
                    return candidate
                return None
            if no_spawn:
                return None
            if self.child is not None and self.child.poll() is None:
                return self.token
            parent_pid = os.getpid()
            proc = subprocess.Popen(
                [sys.executable, str(self.script), "--port", str(self.port)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                cwd=str(self.repo_root),
                preexec_fn=lambda: _die_with_parent(parent_pid))
            self.child = proc
            self.owned = True
        token: Optional[str] = None
        done = threading.Event()
        def drain() -> None:
            nonlocal token
            assert proc.stdout is not None
            for line in proc.stdout:
                if token is None and line.startswith("TOKEN: "):
                    token = line[7:].strip()
                    done.set()
        threading.Thread(target=drain, daemon=True).start()
        if not done.wait(10) or proc.poll() is not None or not token:
            self.fail_child(proc)
            return None
        self.token = token
        return token

    def fail_child(self, proc: Optional[subprocess.Popen] = None) -> None:
        with self._lock:
            if proc is not None and proc is not self.child:
                return
            self.token = None
            if self.child is not None:
                terminate_child(self.child)
            self.child = None
            self.owned = False

    def child_stopped(self) -> bool:
        with self._lock:
            return self.owned and self.child is not None and self.child.poll() is not None

    def cleanup(self) -> None:
        with self._lock:
            proc = self.child if self.owned else None
            self.child = None
            self.token = None
            self.owned = False
        terminate_child(proc)
