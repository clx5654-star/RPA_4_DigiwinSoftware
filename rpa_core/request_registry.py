"""Append-only business request registry and duplicate-prevention gate."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RequestStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


class GateAction(str, Enum):
    ALLOW_WRITE = "ALLOW_WRITE"
    RETURN_CONFIRMED = "RETURN_CONFIRMED"
    RECONCILE_ONLY = "RECONCILE_ONLY"
    REJECT = "REJECT"


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    request_no: str
    campaign_id: str
    attempt: int
    existing: Mapping[str, Any] | None = None
    reason: str = ""


def resolve_request_no(xlsx_value: str | None, cli_value: str | None) -> str:
    xlsx = str(xlsx_value or "").strip()
    cli = str(cli_value or "").strip()
    if xlsx and cli and xlsx != cli:
        raise ValueError(
            f"业务请求号冲突: XLSX={xlsx!r}, --request-no={cli!r}")
    request_no = cli or xlsx
    if not request_no:
        raise ValueError("写流程必须由 XLSX 业务请求号列或 --request-no 提供业务请求号")
    return request_no


class RequestRegistry:
    """A history log; prior transitions are never overwritten or deleted."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _scope(campaign_id: str | None) -> str:
        return str(campaign_id or "").strip() or "NON_CAMPAIGN"

    def read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported request registry schema: {payload.get('schema_version')}")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError("request registry entries must be a list")
        return [dict(entry) for entry in entries]

    def latest(self, request_no: str, campaign_id: str | None,
               dataset_epoch: str | None = None) -> dict | None:
        scope = self._scope(campaign_id)
        epoch = str(dataset_epoch or "").strip()
        matches = [entry for entry in self.read()
                   if entry.get("request_no") == request_no
                   and entry.get("campaign_id") == scope
                   and str(entry.get("dataset_epoch") or "").strip() == epoch]
        return matches[-1] if matches else None

    def decide(self, request_no: str, payload_fingerprint: str,
               campaign_id: str | None, *, retry_not_applied: bool = False,
               dataset_epoch: str | None = None,
               ) -> GateDecision:
        scope = self._scope(campaign_id)
        existing = self.latest(request_no, scope, dataset_epoch)
        if existing is None:
            return GateDecision(
                GateAction.ALLOW_WRITE, request_no, scope, 1,
                reason="new request number")
        if existing.get("payload_fingerprint") != payload_fingerprint:
            return GateDecision(
                GateAction.REJECT, request_no, scope,
                int(existing.get("attempt", 0)), existing,
                "same request number has a different payload fingerprint")
        status = RequestStatus(existing["status"])
        attempt = int(existing.get("attempt", 1))
        if status == RequestStatus.CONFIRMED:
            return GateDecision(
                GateAction.RETURN_CONFIRMED, request_no, scope,
                attempt, existing, "already confirmed")
        if status in {RequestStatus.PENDING, RequestStatus.UNKNOWN}:
            return GateDecision(
                GateAction.RECONCILE_ONLY, request_no, scope,
                attempt, existing, "uncertain prior attempt must be reconciled")
        if not retry_not_applied:
            return GateDecision(
                GateAction.REJECT, request_no, scope, attempt, existing,
                "NOT_APPLIED requires --retry-not-applied")
        return GateDecision(
            GateAction.ALLOW_WRITE, request_no, scope, attempt + 1,
            existing, "explicit retry after NOT_APPLIED")

    def append(self, *, request_no: str, payload_fingerprint: str,
               status: RequestStatus | str, campaign_id: str | None,
               run_id: str | None, attempt: int, doc_no: str | None = None,
               dataset_epoch: str | None = None,
               evidence_jsonl: str | None = None,
               details: Mapping[str, Any] | None = None) -> dict:
        status = RequestStatus(status)
        entries = self.read()
        row = {
            "request_no": request_no,
            "campaign_id": self._scope(campaign_id),
            "dataset_epoch": str(dataset_epoch or "").strip(),
            "payload_fingerprint": payload_fingerprint,
            "status": status.value,
            "run_id": run_id,
            "attempt": int(attempt),
            "doc_no": doc_no,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "evidence_jsonl": evidence_jsonl,
            "details": dict(details or {}),
        }
        entries.append(row)
        payload = {"schema_version": self.SCHEMA_VERSION, "entries": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        return row

    def record_pending(self, decision: GateDecision, payload_fingerprint: str,
                       *, run_id: str, doc_no: str,
                       dataset_epoch: str | None = None,
                       evidence_jsonl: str) -> dict:
        if decision.action != GateAction.ALLOW_WRITE:
            raise ValueError("only ALLOW_WRITE can be registered as PENDING")
        return self.append(
            request_no=decision.request_no,
            campaign_id=decision.campaign_id,
            dataset_epoch=dataset_epoch,
            payload_fingerprint=payload_fingerprint,
            status=RequestStatus.PENDING,
            run_id=run_id,
            attempt=decision.attempt,
            doc_no=doc_no,
            evidence_jsonl=evidence_jsonl,
        )
