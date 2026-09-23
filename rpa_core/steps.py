"""Fail-closed step protocol for E10 write workflows.

The executor deliberately never retries an action.  It records intent before a
business write, sends the action at most once, and resolves uncertainty only by
reconciliation.  UI-specific code supplies the callbacks; this module remains
portable and offline-testable.
"""

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Callable, Mapping, Protocol

from .models import RiskLevel, safe_to_retry_after_failure


class ReconcileStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


class StepStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class WriteIntent:
    request_no: str
    payload_fingerprint: str
    document_key: str
    action: str
    before_values: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ReconcileEvidence:
    status: ReconcileStatus
    source: str
    external: bool
    details: Mapping[str, Any] = field(default_factory=dict)


class JournalLike(Protocol):
    def emit(self, event: str, status: str, message: str, **details): ...


@dataclass(frozen=True)
class StepSpec:
    name: str
    risk: RiskLevel
    precondition: Callable[[Any], None]
    locate: Callable[[Any], Any]
    act: Callable[[Any, Any], None]
    readback: Callable[[Any, Any], Any]
    expect: Callable[[Any, Any], None]
    reconcile: Callable[[Any], ReconcileEvidence] | None = None
    writes_business_data: bool = False
    intent: WriteIntent | None = None
    human_confirmed: bool = False
    require_external_reconcile: bool = False

    def __post_init__(self):
        risk = RiskLevel(self.risk)
        if self.writes_business_data and self.intent is None:
            raise ValueError("write step requires an intent")
        if self.writes_business_data and self.reconcile is None:
            raise ValueError("write step requires reconciliation")
        if risk == RiskLevel.COMMIT and self.writes_business_data and not self.human_confirmed:
            raise ValueError("COMMIT write step requires explicit human confirmation")


@dataclass(frozen=True)
class StepResult:
    status: StepStatus
    message: str
    write_request_issued: bool
    business_data_modified: bool
    safe_to_retry: bool
    auto_retry_allowed: bool
    reconcile_status: ReconcileStatus | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


def auto_retry_allowed(
    risk: RiskLevel,
    *,
    write_request_issued: bool,
    reconcile_status: ReconcileStatus | None = None,
) -> bool:
    """Risk matrix for an engine that may later add automatic retries."""
    risk = RiskLevel(risk)
    if risk == RiskLevel.READ_ONLY:
        return not write_request_issued
    if risk == RiskLevel.REVERSIBLE_WRITE:
        return (not write_request_issued
                or reconcile_status == ReconcileStatus.NOT_APPLIED)
    return False


class StepExecutor:
    """Execute one semantic step without ever repeating ``act``."""

    def __init__(self, journal: JournalLike):
        self.journal = journal

    def execute(self, step: StepSpec, context: Any) -> StepResult:
        started = time.monotonic()
        risk = RiskLevel(step.risk)
        write_issued = False
        try:
            step.precondition(context)
            self.journal.emit("step_precondition", "OK", step.name, step=step.name)
            target = step.locate(context)
            self.journal.emit("step_locate", "OK", step.name, step=step.name)

            if step.writes_business_data:
                intent = step.intent
                self.journal.emit(
                    "write_intent", "PENDING", step.name,
                    step=step.name,
                    request_no=intent.request_no,
                    payload_fingerprint=intent.payload_fingerprint,
                    document_key=intent.document_key,
                    action=intent.action,
                    before_values=dict(intent.before_values),
                    payload=dict(intent.payload),
                    business_data_modified=False,
                    safe_to_retry=True,
                )

            # From this point a write actor may send input before its provider
            # reports an exception.  Mark it issued conservatively *before*
            # entering act(); uncertainty must reconcile and must never retry.
            if step.writes_business_data:
                write_issued = True
            # Exactly one call site for the action.  All uncertainty below is
            # handled by reconcile(); this method never loops back here.
            step.act(context, target)
            self.journal.emit(
                "step_act", "SENT", step.name, step=step.name,
                write_request_issued=write_issued,
            )

            readback = step.readback(context, target)
            step.expect(context, readback)
            self.journal.emit("step_expect", "OK", step.name, step=step.name)

            if not step.writes_business_data:
                return StepResult(
                    StepStatus.SUCCEEDED, "expectation satisfied", False, False,
                    True, auto_retry_allowed(risk, write_request_issued=False),
                )
            return self._reconcile(
                step, context, write_issued,
                step_elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            )
        except Exception as exc:
            self.journal.emit(
                "step_error", "FAILED", str(exc), step=step.name,
                write_request_issued=write_issued,
                step_elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            )
            if write_issued:
                return self._reconcile(step, context, write_issued,
                                       prior_error=str(exc),
                                       step_elapsed_ms=round(
                                           (time.monotonic() - started) * 1000, 3))
            safe = safe_to_retry_after_failure(
                risk, write_request_issued=False, result_unknown=False)
            return StepResult(
                StepStatus.FAILED, str(exc), False, False, safe,
                auto_retry_allowed(risk, write_request_issued=False),
            )

    def _reconcile(
        self,
        step: StepSpec,
        context: Any,
        write_issued: bool,
        *,
        prior_error: str | None = None,
        step_elapsed_ms: float | None = None,
    ) -> StepResult:
        try:
            evidence = step.reconcile(context)
            if not isinstance(evidence, ReconcileEvidence):
                raise TypeError("reconcile must return ReconcileEvidence")
        except Exception as exc:
            evidence = ReconcileEvidence(
                ReconcileStatus.UNKNOWN,
                source="reconcile_error",
                external=False,
                details={"error": str(exc)},
            )

        if (step.require_external_reconcile and not evidence.external
                and evidence.status == ReconcileStatus.CONFIRMED):
            evidence = ReconcileEvidence(
                ReconcileStatus.UNKNOWN,
                source=evidence.source,
                external=False,
                details={**dict(evidence.details),
                         "reason": "external reconciliation required"},
            )

        status = evidence.status
        modified = status == ReconcileStatus.CONFIRMED
        unknown = status == ReconcileStatus.UNKNOWN
        safe = safe_to_retry_after_failure(
            step.risk,
            write_request_issued=write_issued,
            result_unknown=unknown or modified,
        )
        auto = auto_retry_allowed(
            step.risk,
            write_request_issued=write_issued,
            reconcile_status=status,
        )
        succeeded = status == ReconcileStatus.CONFIRMED
        message = {
            ReconcileStatus.CONFIRMED: "write confirmed",
            ReconcileStatus.NOT_APPLIED: "write confirmed not applied",
            ReconcileStatus.UNKNOWN: "write result unknown; manual handling required",
        }[status]
        self.journal.emit(
            "reconcile",
            "OK" if succeeded else "FAILED",
            message,
            step=step.name,
            reconcile_status=status.value,
            reconcile_source=evidence.source,
            external=evidence.external,
            reconcile_details=dict(evidence.details),
            prior_error=prior_error,
            business_data_modified=modified,
            safe_to_retry=safe,
            auto_retry_allowed=auto,
            step_elapsed_ms=step_elapsed_ms,
        )
        return StepResult(
            StepStatus.SUCCEEDED if succeeded else StepStatus.FAILED,
            message,
            write_issued,
            modified,
            safe,
            auto,
            status,
            dict(evidence.details),
        )
