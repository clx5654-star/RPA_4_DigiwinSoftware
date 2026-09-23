"""Declarative metadata and offline checks for requisition workflows.

This module describes *existing* semantic actions.  It does not execute UIA
operations and is deliberately kept independent from Windows libraries.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from .models import QueryOutcome, QueryStatus, RiskLevel
from .identity import IdentityStrength
from .steps import ReconcileEvidence, ReconcileStatus


@dataclass(frozen=True)
class StepDescriptor:
    name: str
    description: str
    risk: RiskLevel
    required: bool
    modifies_business_data: bool
    retryable: bool
    opened_windows: tuple[str, ...] = ()
    closed_windows: tuple[str, ...] = ()
    required_when: str | None = None


@dataclass(frozen=True)
class WorkflowDescriptor:
    workflow_id: str
    steps: tuple[StepDescriptor, ...]

    def step(self, name: str) -> StepDescriptor:
        matches = [step for step in self.steps if step.name == name]
        if len(matches) != 1:
            raise KeyError(f"workflow {self.workflow_id!r} has no unique step {name!r}")
        return matches[0]


def _step(name, description, risk=RiskLevel.REVERSIBLE_WRITE, *, required=True,
          modifies=False, retryable=True, opens=(), closes=(), required_when=None):
    return StepDescriptor(
        name=name,
        description=description,
        risk=RiskLevel(risk),
        required=required,
        modifies_business_data=modifies,
        retryable=retryable,
        opened_windows=tuple(opens),
        closed_windows=tuple(closes),
        required_when=required_when,
    )


_HEADER_AND_ROW = (
    _step("select_document_type", "选择单据类型并读回"),
    _step("select_applicant", "选择申请人并读回"),
    _step("set_required_date", "写入需求日期并读回"),
    _step("select_item", "用 F2 查询品号并唯一选择"),
    _step("set_quantity", "写入请购数量并读回"),
    _step(
        "verify_warehouse", "验证仓库输入或记录 E10 自动默认值",
        required=False, required_when="warehouse_provided",
    ),
)


WORKFLOWS: dict[str, WorkflowDescriptor] = {
    "e10.requisition.create.end_to_end": WorkflowDescriptor(
        "e10.requisition.create.end_to_end",
        (
            _step("ensure_ready", "确认唯一 FRKTEST 主窗口且无遗留业务窗",
                  RiskLevel.READ_ONLY),
            _step("open_new", "进入维护请购单并新建空白单",
                  opens=("浏览 - 维护请购单", "维护请购单")),
            *_HEADER_AND_ROW[:3],
            _step("set_correlation_field",
                  "把业务请求号写入备注并读回，作为 E10 内稳定关联标记"),
            *_HEADER_AND_ROW[3:],
            _step("validate", "单击普通校验并再次读回关键字段"),
            _step(
                "save_requisition_once", "记录 intent、保存一次并按单号核对",
                RiskLevel.COMMIT, modifies=True, retryable=False,
                closes=("维护请购单", "浏览 - 维护请购单"),
            ),
            _step("cleanup", "只关闭本次运行拥有的剩余窗口",
                  RiskLevel.READ_ONLY),
        ),
    ),
    "e10.requisition.create.fill": WorkflowDescriptor(
        "e10.requisition.create.fill",
        (_step("preflight", "验证当前为空白新单", RiskLevel.READ_ONLY),
         *_HEADER_AND_ROW),
    ),
    "e10.requisition.create.resume_fill": WorkflowDescriptor(
        "e10.requisition.create.resume_fill",
        (_step("resume_precondition", "确认可接管的表头草稿", RiskLevel.READ_ONLY),
         *_HEADER_AND_ROW[3:]),
    ),
    "e10.requisition.create.resume_open_item": WorkflowDescriptor(
        "e10.requisition.create.resume_open_item",
        (_step("resume_precondition", "确认普通品号查询窗和草稿", RiskLevel.READ_ONLY),
         *_HEADER_AND_ROW[3:]),
    ),
    "e10.requisition.create.resume_quantity": WorkflowDescriptor(
        "e10.requisition.create.resume_quantity",
        (_step("resume_precondition", "确认品号已精确回填", RiskLevel.READ_ONLY),
         *_HEADER_AND_ROW[4:]),
    ),
    "e10.requisition.verify": WorkflowDescriptor(
        "e10.requisition.verify",
        (
            _step("ensure_ready", "确认唯一 FRKTEST 主窗口且无遗留业务窗",
                  RiskLevel.READ_ONLY),
            _step("open_browse", "进入维护请购单浏览窗口", RiskLevel.READ_ONLY,
                  opens=("浏览 - 维护请购单",)),
            _step("query_by_document_no", "按单号精确查询并读取状态区",
                  RiskLevel.READ_ONLY),
            _step("cleanup", "关闭本次运行拥有的窗口", RiskLevel.READ_ONLY,
                  closes=("浏览 - 维护请购单",)),
        ),
    ),
}


def get_workflow(workflow_id: str) -> WorkflowDescriptor:
    try:
        return WORKFLOWS[workflow_id]
    except KeyError as exc:
        raise KeyError(f"unknown requisition workflow: {workflow_id}") from exc


def workflow_plan(workflow_id: str, *, warehouse_provided: bool = False) -> list[dict]:
    result = []
    for step in get_workflow(workflow_id).steps:
        skipped = step.required_when == "warehouse_provided" and not warehouse_provided
        required = step.required or (
            step.required_when == "warehouse_provided" and warehouse_provided)
        result.append({
            "step": step.name,
            "description": step.description,
            "risk": step.risk.value,
            "required": required,
            "modifies_business_data": step.modifies_business_data,
            "retryable": step.retryable,
            "opened_windows": list(step.opened_windows),
            "closed_windows": list(step.closed_windows),
            "status": "SKIPPED" if skipped else "PLANNED",
            "reason": "not_in_input_e10_auto_default" if skipped else None,
        })
    return result


def validate_step_sequence(
    workflow_id: str,
    records: Iterable[Mapping[str, Any]],
    *,
    successful: bool,
    warehouse_provided: bool = False,
) -> tuple[str, ...]:
    """Validate names, relative order and conditional required coverage."""
    descriptor = get_workflow(workflow_id)
    positions = {step.name: index for index, step in enumerate(descriptor.steps)}
    observed: list[str] = []
    statuses: dict[str, set[str]] = {}
    last_position = -1
    for record in records:
        details = record.get("details") or {}
        name = details.get("step")
        if not name:
            continue
        if name not in positions:
            raise ValueError(f"step {name!r} is not declared for {workflow_id}")
        statuses.setdefault(name, set()).add(str(record.get("status", "")))
        if observed and observed[-1] == name:
            continue
        position = positions[name]
        if position < last_position:
            raise ValueError(f"step order regression: {name!r}")
        observed.append(name)
        last_position = position

    if successful:
        required = {
            step.name for step in descriptor.steps
            if step.required or (
                step.required_when == "warehouse_provided" and warehouse_provided)
        }
        missing = sorted(required - set(observed))
        if missing:
            raise ValueError(f"successful run missing required steps: {missing}")
        if not warehouse_provided:
            skipped = statuses.get("verify_warehouse", set())
            if "SKIPPED" not in skipped:
                raise ValueError("warehouse omission requires explicit SKIPPED evidence")
    return tuple(observed)


def validate_commit_cardinality(records: Iterable[Mapping[str, Any]], *, saved: bool) -> None:
    records = list(records)
    intents = [row for row in records if row.get("event") == "write_intent"]
    acts = [row for row in records if (
        row.get("event") == "step_act"
        and (row.get("details") or {}).get("write_request_issued") is True
    )]
    if len(intents) > 1 or len(acts) > 1:
        raise ValueError("write intent or issued write request occurred more than once")
    if saved and (len(intents) != 1 or len(acts) != 1):
        raise ValueError("saved run requires exactly one intent and one issued write request")
    if intents and acts and records.index(intents[0]) >= records.index(acts[0]):
        raise ValueError("write_intent must precede the issued write request")


def validate_window_transitions(
    workflow_id: str,
    transitions: Iterable[Mapping[str, Any]],
    *,
    remaining_owned_windows: Iterable[str] = (),
) -> None:
    descriptor = get_workflow(workflow_id)
    allowed_open = {name for step in descriptor.steps for name in step.opened_windows}
    allowed_close = {name for step in descriptor.steps for name in step.closed_windows}
    for transition in transitions:
        action = transition.get("action")
        title = str(transition.get("title", ""))
        allowed = allowed_open if action == "opened" else allowed_close
        if action not in {"opened", "closed"} or title not in allowed:
            raise ValueError(f"undeclared window transition: {transition}")
    remaining = tuple(remaining_owned_windows)
    if remaining:
        raise ValueError(f"owned windows remain at successful terminal state: {remaining}")


def query_outcome_to_reconcile(outcome: QueryOutcome) -> ReconcileEvidence:
    details = {
        "query_status": outcome.status.value,
        "query_code": outcome.code,
        "query_message": outcome.message,
        **dict(outcome.details or {}),
    }
    if outcome.status == QueryStatus.FOUND:
        status = ReconcileStatus.CONFIRMED
    elif outcome.status == QueryStatus.EMPTY:
        status = ReconcileStatus.NOT_APPLIED
    else:
        status = ReconcileStatus.UNKNOWN
    return ReconcileEvidence(status, "E10_UI_TEST_DATABASE", False, details)


def enforce_identity_strength(evidence: ReconcileEvidence,
                              strength: IdentityStrength | str,
                              ) -> ReconcileEvidence:
    """A bare recycled document number can never confirm run identity."""
    strength = IdentityStrength(strength)
    if (evidence.status == ReconcileStatus.CONFIRMED
            and strength == IdentityStrength.DOC_NO_ONLY):
        return ReconcileEvidence(
            ReconcileStatus.UNKNOWN,
            evidence.source,
            evidence.external,
            {
                **dict(evidence.details),
                "reason": (
                    "document number matched, but the test database can reuse "
                    "numbers; run identity is unproven"),
                "identity_strength": strength.value,
            },
        )
    return evidence


class VerifyActor(Protocol):
    def open(self) -> None: ...
    def query(self, document_no: str) -> QueryOutcome: ...
    def cleanup(self) -> None: ...


def execute_verify_protocol(actor: VerifyActor, document_no: str) -> ReconcileEvidence:
    """Read-only coordinator.  It has no create/fill/validate/save operation."""
    if not str(document_no).strip():
        raise ValueError("document_no is required")
    try:
        actor.open()
        outcome = actor.query(str(document_no).strip())
        evidence = query_outcome_to_reconcile(outcome)
    except Exception as exc:
        evidence = ReconcileEvidence(
            ReconcileStatus.UNKNOWN,
            "E10_UI_TEST_DATABASE",
            False,
            {"reason": f"verify failed: {type(exc).__name__}: {exc}"},
        )
    finally:
        try:
            actor.cleanup()
        except Exception as exc:
            evidence = ReconcileEvidence(
                ReconcileStatus.UNKNOWN,
                "E10_UI_TEST_DATABASE",
                False,
                {**dict(evidence.details),
                 "cleanup_error": f"{type(exc).__name__}: {exc}"},
            )
    return evidence
