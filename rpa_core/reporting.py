import json
import atexit
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .instrumentation import FailureCode, ObservationCode, classify_failure


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class RunJournal:
    """Append-only JSONL evidence for one workflow run."""

    SCHEMA_VERSION = 2

    _active = weakref.WeakSet()

    def __init__(self, directory, *, workflow_id, workflow_version,
                 selector_version, risk, environment=None, initial_state=None,
                 terminal_observer=None):
        self.run_id = uuid4().hex
        self.workflow_id = workflow_id
        self.workflow_version = workflow_version
        self.selector_version = selector_version
        self.risk = getattr(risk, "value", str(risk))
        self.environment = dict(environment or {})
        self._initial_state = dict(initial_state or {})
        self._finished = False
        self._corrections = {
            "window_recovery": 0,
            "navigation_scroll": 0,
            "dropdown_fallback": 0,
            "scope_reresolve": 0,
        }
        self._foreign_foreground_observed = 0
        self._terminal_observer = terminal_observer
        self._run_started_monotonic = time.monotonic()
        self._last_semantic_completion = self._run_started_monotonic
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.run_id}.jsonl"
        self._active.add(self)
        self.emit(
            "run_started", "RUNNING", "RPA run started",
            business_data_modified=False, safe_to_retry=True,
            initial_state=dict(initial_state or {}),
            human_intervention={
                "declared_no_intervention": bool(
                    self.environment.get("attest_no_intervention")),
                "foreign_foreground_observed": 0,
            },
        )

    def emit(self, event: str, status: str, message: str, **details):
        now_monotonic = time.monotonic()
        business_data_modified = details.pop("business_data_modified", None)
        safe_to_retry = details.pop("safe_to_retry", None)
        if (details.get("step") and "step_elapsed_ms" not in details
                and event in {"field", "row_readback", "validation", "cleanup",
                              "step_expect", "step_error", "step", "query_result"}):
            details["step_elapsed_ms"] = round(
                (now_monotonic - self._last_semantic_completion) * 1000, 3)
            self._last_semantic_completion = now_monotonic
        if event == "window_recovery":
            self._corrections["window_recovery"] += 1
        elif event == "navigation_scroll":
            self._corrections["navigation_scroll"] += 1
        elif event == "dropdown_capability_probe" and (
                status == "FALLBACK" or details.get("capability") == "FALLBACK"):
            self._corrections["dropdown_fallback"] += 1
        elif event in {"scope_reresolve", "control_reresolve"}:
            self._corrections["scope_reresolve"] += 1

        # Terminal failures and recoverable observations use separate
        # namespaces.  A failed wait must never masquerade as the run result.
        if event == "wait_timing" and status == "FAILED":
            details.setdefault(
                "observation_code", ObservationCode.WAIT_TIMEOUT.value)
        elif event in {"navigation_scroll", "dropdown_capability_probe"}:
            details.setdefault(
                "observation_code", ObservationCode.NAV_FALLBACK_USED.value)
        if event == "run_finished":
            if self._terminal_observer is not None:
                try:
                    observed = dict(self._terminal_observer() or {})
                    self._foreign_foreground_observed += int(
                        observed.get("foreign_foreground_observed", 0))
                except Exception as exc:
                    details["terminal_observer_error"] = str(exc)
            if status == "INTERRUPTED":
                details["failure_code"] = FailureCode.INTERRUPTED.value
            elif status in {"FAILED", "UNKNOWN", "SAVE_UNKNOWN_OR_FAILED"}:
                if not details.get("failure_code"):
                    classified = classify_failure(
                        message, error_type=details.get("error_type"))
                    if (classified == FailureCode.PREEXISTING_UNKNOWN_WINDOW
                            and not self._initial_state.get(
                                "unknown_windows")):
                        classified = FailureCode.SCENE_NOT_CONFORMANT
                    details["failure_code"] = classified.value
            details.setdefault("corrections", dict(self._corrections))
            details.setdefault(
                "run_elapsed_ms",
                round((now_monotonic - self._run_started_monotonic) * 1000, 3))
            details.setdefault("human_intervention", {
                "declared_no_intervention": bool(
                    self.environment.get("attest_no_intervention")),
                "foreign_foreground_observed": self._foreign_foreground_observed,
            })
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "timestamp": _utc_now(),
            "workflow": {"id": self.workflow_id, "version": self.workflow_version},
            "selector_version": self.selector_version,
            "risk": self.risk,
            "event": event,
            "status": status,
            "message": message,
            "business_data_modified": business_data_modified,
            "safe_to_retry": safe_to_retry,
            "environment": self.environment,
            "details": details,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if event == "run_finished":
            self._finished = True
            self._active.discard(self)
        return record

    def note_foreign_foreground(self, count: int = 1) -> None:
        self._foreign_foreground_observed += max(0, int(count))

    def interrupt_if_open(self, message: str = "run ended without terminal event") -> None:
        if self._finished:
            return
        try:
            self.emit(
                "run_finished", "INTERRUPTED", message,
                business_data_modified=False, safe_to_retry=False,
                failure_code=FailureCode.INTERRUPTED.value,
            )
        except OSError:
            # A temporary test directory can disappear before interpreter exit.
            self._finished = True
            self._active.discard(self)


def _finalize_active_journals() -> None:
    for journal in list(RunJournal._active):
        journal.interrupt_if_open()


atexit.register(_finalize_active_journals)
