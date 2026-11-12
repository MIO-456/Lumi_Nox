"""One deadline and cancellation boundary for a Kingdom Rush launch."""

import subprocess
import threading
import time
import uuid


KR_STARTUP_TIMEOUT = 60.0
KR_DIRECTOR_START_TIMEOUT = 70


class StartupCancelled(RuntimeError):
    pass


class StartupTimeout(RuntimeError):
    pass


class StartupControl:
    def __init__(self, timeout=KR_STARTUP_TIMEOUT):
        self.session_id = uuid.uuid4().hex
        self.deadline = time.monotonic() + timeout
        self.cancel_event = threading.Event()
        self.finished = threading.Event()
        self.ready = False
        self.error = ""
        self._lock = threading.RLock()
        self._process = None

    @property
    def process_id(self):
        with self._lock:
            return self._process.pid if self._process is not None else None

    def check(self):
        if self.cancel_event.is_set():
            raise StartupCancelled(self.error or "王国保卫战启动已取消")
        if not self.ready and time.monotonic() >= self.deadline:
            raise StartupTimeout("王国保卫战启动超时（总预算 60 秒）")

    def remaining(self, cap=None):
        self.check()
        remaining = max(0.0, self.deadline - time.monotonic())
        return remaining if cap is None else min(remaining, cap)

    def wait(self, seconds):
        self.cancel_event.wait(self.remaining(seconds))
        self.check()

    def run(self, callback, *args, **kwargs):
        # Only short operations belong here. Cancellation can interrupt all waits.
        with self._lock:
            self.check()
            return callback(*args, **kwargs)

    def spawn(self, args, **kwargs):
        def start():
            self._process = subprocess.Popen(args, **kwargs)
            return self._process
        return self.run(start)

    def mark_ready(self):
        def mark():
            self.ready = True
            self.finished.set()
        self.run(mark)

    def fail(self, error):
        with self._lock:
            if not self.ready and not self.error:
                self.error = str(error)
            self.finished.set()

    def cancel(self, kill_game=True):
        with self._lock:
            self.cancel_event.set()
            self.finished.set()
            process = self._process if kill_game else None
        # A Popen handle identifies only this launch, never all matching exe names.
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                if process.poll() is None:
                    raise
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
