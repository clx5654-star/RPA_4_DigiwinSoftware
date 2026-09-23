"""Pure domain primitives shared by E10 UI automation workflows.

This package deliberately has no dependency on pywinauto, ctypes or Windows.
It can therefore be tested without starting or touching E10.
"""

from .grid import GridObservation, GridSnapshot, classify_query_result
from .dropdown import (
    DropdownAction,
    DropdownDecision,
    DropdownDecisionError,
    DropdownScrollJump,
    DropdownStalled,
    DropdownTargetSkipped,
    decide_dropdown_action,
)
from .models import (QueryOutcome, QueryStatus, RiskLevel, WindowIdentity,
                     safe_to_retry_after_failure)
from .ownership import WindowOwnership
from .reporting import RunJournal
from .requisition import REQUIRED_HEADERS, RequisitionRecord
from .selectors import AmbiguousSelector, SelectorNotFound, require_unique
from .steps import (ReconcileEvidence, ReconcileStatus, StepExecutor, StepResult,
                    StepSpec, StepStatus, WriteIntent, auto_retry_allowed)
from .heartbeat import ProgressHeartbeat
from .ledger import (derive_ledger, load_manual_registrations,
                     merge_manual_registrations,
                     scaffold_manual_registrations, validate_ledger)
from .campaign import build_campaign_report
from .instrumentation import (FailureCode, ObservationCode, classify_failure, compute_code_hash,
                              validate_terminal_contract)
from .workflows import (StepDescriptor, WorkflowDescriptor,
                        execute_verify_protocol, get_workflow,
                        enforce_identity_strength,
                        query_outcome_to_reconcile, validate_commit_cardinality,
                        validate_step_sequence, validate_window_transitions,
                        workflow_plan)
from .windowing import (WindowRecoveryError, WindowRecoveryPlan,
                        plan_window_recovery)
from .request_registry import (GateAction, GateDecision, RequestRegistry,
                               RequestStatus, resolve_request_no)
from .desktop import (DesktopContext, DesktopGuardDecision,
                      evaluate_desktop_context)
from .identity import (IdentityStrength, correlation_value, identity_key,
                       registration_key, require_dataset_epoch)
from .startup import OrphanAction, audit_orphaned_runs
from .login import (LoginDecision, LoginObservation, LoginPhase,
                    classify_login_observation)

__all__ = [
    "AmbiguousSelector",
    "DropdownAction",
    "DropdownDecision",
    "DropdownDecisionError",
    "DropdownScrollJump",
    "DropdownStalled",
    "DropdownTargetSkipped",
    "DesktopContext",
    "DesktopGuardDecision",
    "GridObservation",
    "GridSnapshot",
    "IdentityStrength",
    "LoginDecision",
    "LoginObservation",
    "LoginPhase",
    "GateAction",
    "GateDecision",
    "FailureCode",
    "ObservationCode",
    "OrphanAction",
    "QueryOutcome",
    "QueryStatus",
    "ProgressHeartbeat",
    "REQUIRED_HEADERS",
    "ReconcileEvidence",
    "ReconcileStatus",
    "RequisitionRecord",
    "RiskLevel",
    "RequestRegistry",
    "RequestStatus",
    "RunJournal",
    "SelectorNotFound",
    "StepExecutor",
    "StepDescriptor",
    "StepResult",
    "StepSpec",
    "StepStatus",
    "WindowIdentity",
    "WindowRecoveryError",
    "WindowRecoveryPlan",
    "WorkflowDescriptor",
    "safe_to_retry_after_failure",
    "WindowOwnership",
    "WriteIntent",
    "auto_retry_allowed",
    "audit_orphaned_runs",
    "build_campaign_report",
    "classify_query_result",
    "classify_login_observation",
    "classify_failure",
    "correlation_value",
    "compute_code_hash",
    "derive_ledger",
    "decide_dropdown_action",
    "require_unique",
    "resolve_request_no",
    "execute_verify_protocol",
    "enforce_identity_strength",
    "evaluate_desktop_context",
    "get_workflow",
    "identity_key",
    "load_manual_registrations",
    "merge_manual_registrations",
    "query_outcome_to_reconcile",
    "validate_commit_cardinality",
    "validate_ledger",
    "validate_terminal_contract",
    "validate_step_sequence",
    "validate_window_transitions",
    "registration_key",
    "require_dataset_epoch",
    "scaffold_manual_registrations",
    "workflow_plan",
    "plan_window_recovery",
]
