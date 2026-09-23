"""Console progress and liveness heartbeat without any UIA calls."""

import threading
import time
from typing import Callable


class ProgressHeartbeat:
    def __init__(self, *, interval: float = 10.0, output: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic,
                 event_sink: Callable[[str, str, float], None] | None = None):
        if interval <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.interval = interval
        self.output = output
        self.clock = clock
        self.event_sink = event_sink
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._current: str | None = None
        self._started = 0.0
        self._last_success: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="e10-rpa-heartbeat", daemon=True)
        self._thread.start()

    def begin_step(self, name: str) -> None:
        with self._lock:
            self._current = name
            self._started = self.clock()
        self.output(f"[RUN] {name} started")

    def complete_step(self, name: str) -> None:
        with self._lock:
            elapsed = self.clock() - self._started if self._current == name else 0.0
            self._last_success = name
            self._current = None
        self.output(f"[OK] {name} elapsed={elapsed:.1f}s")
        if self.event_sink:
            self.event_sink(name, "OK", elapsed * 1000)

    def skip_step(self, name: str, reason: str) -> None:
        with self._lock:
            self._last_success = name
            self._current = None
        self.output(f"[SKIPPED] {name} reason={reason}")
        if self.event_sink:
            self.event_sink(name, "SKIPPED", 0.0)

    def fail_step(self, error: BaseException) -> None:
        with self._lock:
            name = self._current or "unknown"
            elapsed = self.clock() - self._started if self._current else 0.0
            self._current = None
        self.output(
            f"[FAILED] {name} elapsed={elapsed:.1f}s "
            f"error={type(error).__name__}: {error}")
        if self.event_sink:
            self.event_sink(name, "FAILED", elapsed * 1000)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, min(self.interval + 0.5, 3.0)))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                current = self._current
                started = self._started
                last_success = self._last_success
            if current is None:
                continue
            elapsed = self.clock() - started
            self.output(
                f"[HEARTBEAT] step={current} elapsed={elapsed:.0f}s "
                f"last_success={last_success or '-'} completion_evidence=none; "
                "Python process alive, current call has not returned")
