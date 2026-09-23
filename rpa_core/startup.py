"""Pure startup audit for interrupted requisition runs."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from .ledger import read_jsonl


class OrphanAction(str, Enum):
    RECONCILE_ONLY = "RECONCILE_ONLY"
    PRE_SAVE_DRAFT_CHECK_REQUIRED = "PRE_SAVE_DRAFT_CHECK_REQUIRED"


def audit_orphaned_runs(run_root: Path, *, request_no: str,
                        dataset_epoch: str,
                        registry_entries: Iterable[Mapping]) -> list[dict]:
    """Return unresolved no-terminal runs for one stable request identity."""
    registry = list(registry_entries)
    resolved = set()
    candidates = []
    for path in sorted(Path(run_root).rglob("*.jsonl")):
        rows = read_jsonl(path)
        if not rows:
            continue
        for row in rows:
            if row.get("event") in {
                    "orphan_resolution", "startup_reconciliation"}:
                resolved.update((row.get("details") or {}).get(
                    "resolved_orphan_run_ids") or [])
        first = rows[0]
        environment = first.get("environment") or {}
        if (str(environment.get("request_no") or "") != request_no
                or str(environment.get("dataset_epoch") or "")
                != dataset_epoch):
            continue
        if any(row.get("event") == "run_finished" for row in rows):
            continue
        candidates.append({
            "run_id": first.get("run_id"),
            "evidence_jsonl": str(path.resolve()),
            "input_file": environment.get("input_file"),
            "input_hash": environment.get("input_hash"),
            "payload_fingerprint": environment.get("payload_fingerprint"),
        })
    output = []
    for candidate in candidates:
        run_id = candidate.get("run_id")
        if run_id in resolved:
            continue
        related = [row for row in registry if row.get("run_id") == run_id]
        uncertain = [row for row in related if row.get("status") in {
            "PENDING", "UNKNOWN"}]
        output.append({
            **candidate,
            "action": (
                OrphanAction.RECONCILE_ONLY.value if uncertain
                else OrphanAction.PRE_SAVE_DRAFT_CHECK_REQUIRED.value),
            "registry_entries": uncertain,
        })
    return output
