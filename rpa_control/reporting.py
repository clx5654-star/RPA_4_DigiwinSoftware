"""Append-only receiver evidence, separate from child RPA journals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReceiverJournal:
    def __init__(self, evidence_dir: Path, *, job_id: str, worker_id: str,
                 lease_generation: int):
        self.path = Path(evidence_dir) / "receiver.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_generation = int(lease_generation)

    def emit(self, event: str, **details: Any) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "timestamp": utc_now(),
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "lease_generation": self.lease_generation,
            "event": event,
            "details": details,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            stream.flush()
        return record
