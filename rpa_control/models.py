"""Strict, transport-neutral job protocol for the local RPA control plane."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 1


class TaskProtocolError(ValueError):
    pass


class TaskRisk(str, Enum):
    READ_ONLY = "READ_ONLY"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    COMMIT = "COMMIT"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.UNKNOWN,
    JobStatus.REQUIRES_HUMAN,
    JobStatus.CANCELLED,
})

LEGAL_TRANSITIONS = MappingProxyType({
    JobStatus.QUEUED: frozenset({JobStatus.LEASED, JobStatus.CANCELLED}),
    JobStatus.LEASED: frozenset({
        JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.UNKNOWN,
        JobStatus.REQUIRES_HUMAN, JobStatus.CANCELLED, JobStatus.QUEUED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.UNKNOWN: frozenset(),
    JobStatus.REQUIRES_HUMAN: frozenset(),
    JobStatus.CANCELLED: frozenset(),
})

FORBIDDEN_KEYS = frozenset({
    "password", "password_env_value", "command", "shell", "script_path",
    "executable", "raw_argv",
})

TASK_FIELDS = frozenset({
    "schema_version", "job_id", "workflow_id", "workflow_version",
    "environment_profile", "risk", "request_no", "idempotency_key",
    "input", "timeout_seconds", "requested_by", "created_at",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_forbidden(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in FORBIDDEN_KEYS:
                raise TaskProtocolError(f"任务包含禁止字段 {path}.{key}")
            _scan_forbidden(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_forbidden(child, f"{path}[{index}]")


def validate_transition(old: JobStatus, new: JobStatus) -> None:
    old = JobStatus(old)
    new = JobStatus(new)
    if new not in LEGAL_TRANSITIONS[old]:
        raise TaskProtocolError(f"非法任务状态转换: {old.value} -> {new.value}")


@dataclass(frozen=True)
class TaskRequest:
    schema_version: int
    job_id: str
    workflow_id: str
    workflow_version: str
    environment_profile: str
    risk: TaskRisk
    request_no: str | None
    idempotency_key: str
    input: Mapping[str, Any]
    timeout_seconds: int
    requested_by: str
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise TaskProtocolError(
                f"不支持 schema_version={self.schema_version}")
        for label, value in (
                ("job_id", self.job_id), ("workflow_id", self.workflow_id),
                ("workflow_version", self.workflow_version),
                ("environment_profile", self.environment_profile),
                ("idempotency_key", self.idempotency_key),
                ("requested_by", self.requested_by),
                ("created_at", self.created_at)):
            if not str(value or "").strip():
                raise TaskProtocolError(f"任务字段 {label} 不能为空")
        if not isinstance(self.input, Mapping):
            raise TaskProtocolError("任务 input 必须是对象")
        if not 1 <= int(self.timeout_seconds) <= 86400:
            raise TaskProtocolError("timeout_seconds 必须在 1..86400")
        try:
            parsed = datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise TaskProtocolError("created_at 必须是 ISO-8601") from exc
        if parsed.tzinfo is None:
            raise TaskProtocolError("created_at 必须包含时区")
        _scan_forbidden(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk"] = self.risk.value
        payload["input"] = dict(self.input)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TaskRequest":
        if not isinstance(payload, Mapping):
            raise TaskProtocolError("任务必须是 JSON 对象")
        unknown = set(payload).difference(TASK_FIELDS)
        missing = TASK_FIELDS.difference(payload)
        if unknown:
            raise TaskProtocolError(f"任务包含未知字段: {sorted(unknown)}")
        if missing:
            raise TaskProtocolError(f"任务缺少字段: {sorted(missing)}")
        _scan_forbidden(payload)
        try:
            risk = TaskRisk(str(payload["risk"]))
        except ValueError as exc:
            raise TaskProtocolError(f"未知风险等级: {payload.get('risk')}") from exc
        return cls(
            schema_version=int(payload["schema_version"]),
            job_id=str(payload["job_id"]),
            workflow_id=str(payload["workflow_id"]),
            workflow_version=str(payload["workflow_version"]),
            environment_profile=str(payload["environment_profile"]),
            risk=risk,
            request_no=(None if payload["request_no"] is None
                        else str(payload["request_no"])),
            idempotency_key=str(payload["idempotency_key"]),
            input=dict(payload["input"]),
            timeout_seconds=int(payload["timeout_seconds"]),
            requested_by=str(payload["requested_by"]),
            created_at=str(payload["created_at"]),
        )


def validate_task_payload(payload: Mapping[str, Any]) -> TaskRequest:
    return TaskRequest.from_dict(payload)
