"""Derive evidence facts and merge separately maintained human registrations."""

import json
from pathlib import Path
from typing import Iterable, Mapping

from .identity import IdentityStrength, registration_key


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL {path}:{number}: {exc}") from exc
    return rows


def ledger_entry(path: Path, rows: Iterable[dict]) -> dict | None:
    """Derive machine evidence only; never invent owner or cleanup decisions."""
    rows = list(rows)
    if not rows:
        return None
    first = rows[0]
    environment = first.get("environment", {})
    workflow_id = (first.get("workflow") or {}).get("id", "")
    if not workflow_id.startswith("e10.requisition."):
        return None
    if workflow_id in {"e10.requisition.verify", "e10.requisition.request_gate"}:
        return None
    intents = [row for row in rows if row.get("event") == "write_intent"]
    issued = [row for row in rows if (
        row.get("event") == "step_act"
        and (row.get("details") or {}).get("write_request_issued") is True)]
    finished = next((row for row in reversed(rows)
                     if row.get("event") == "run_finished"), None)
    reconcile = next((row for row in reversed(rows)
                      if row.get("event") == "reconcile"), None)
    intent_details = (intents[-1].get("details") or {}) if intents else {}
    before = intent_details.get("before_values") or {}
    finish_details = (finished.get("details") or {}) if finished else {}
    reconcile_details = (reconcile.get("details") or {}) if reconcile else {}
    doc_no = (finish_details.get("doc_no") or intent_details.get("document_key")
              or before.get("doc_no_preallocated"))
    status = reconcile_details.get("reconcile_status")
    persisted = True if status == "CONFIRMED" else False if status == "NOT_APPLIED" else None
    fingerprint = intent_details.get("payload_fingerprint")
    fingerprint_source = "payload_fingerprint"
    if not fingerprint and intent_details.get("idempotency_key"):
        fingerprint = intent_details.get("idempotency_key")
        fingerprint_source = "legacy_idempotency_key"
    request_no = (finish_details.get("request_no")
                  or intent_details.get("request_no")
                  or environment.get("request_no"))
    dataset_epoch = (finish_details.get("dataset_epoch")
                     or environment.get("dataset_epoch"))
    identity_strength = (finish_details.get("identity_strength")
                         or environment.get("identity_strength")
                         or IdentityStrength.DOC_NO_ONLY.value)
    document_identity_key = (finish_details.get("document_identity_key")
                             or environment.get("document_identity_key"))
    entry = {
        "run_id": first.get("run_id"),
        "workflow_id": workflow_id,
        "input_file": environment.get("input_file"),
        "input_hash": environment.get("input_hash"),
        "campaign_id": environment.get("campaign_id", ""),
        "request_no": request_no,
        "dataset_epoch": dataset_epoch,
        "document_identity_key": document_identity_key,
        "identity_strength": identity_strength,
        "correlation_field": (finish_details.get("correlation_field")
                              or environment.get("correlation_field")),
        "correlation_value": (finish_details.get("correlation_value")
                              or environment.get("correlation_value")),
        "payload_fingerprint": fingerprint,
        "payload_fingerprint_source": fingerprint_source if fingerprint else None,
        "doc_no": doc_no,
        "doc_no_preallocated": before.get("doc_no_preallocated"),
        "persisted": persisted,
        "write_request_issued": bool(issued),
        "reconcile_status": status or finish_details.get("reconcile_status"),
        "created_at": first.get("timestamp"),
        "evidence_jsonl": str(path.resolve()),
        "evidence_gaps": [name for name, value in (
            ("input_file", environment.get("input_file")),
            ("input_hash", environment.get("input_hash")),
            ("request_no", request_no),
        ) if value is None],
    }
    entry["manual_registration_key"] = registration_key(entry)
    return entry


def derive_ledger(run_root: Path) -> list[dict]:
    entries = []
    for path in sorted(run_root.rglob("*.jsonl")):
        entry = ledger_entry(path, read_jsonl(path))
        if entry and (entry["doc_no"] or entry["write_request_issued"]):
            entries.append(entry)
    return entries


def load_manual_registrations(path: Path) -> dict[str, dict]:
    if not Path(path).is_file():
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    documents = payload.get("documents", {})
    if not isinstance(documents, dict):
        raise ValueError(
            "manual registration documents must be an object keyed by stable registration key")
    return {str(key): dict(value) for key, value in documents.items()}


def scaffold_manual_registrations(path: Path,
                                  entries: Iterable[dict]) -> dict:
    """Add missing stable-key slots without overwriting any human value."""
    path = Path(path)
    existing_payload = {}
    if path.is_file():
        existing_payload = json.loads(path.read_text(encoding="utf-8"))
    existing = existing_payload.get("documents", {})
    if not isinstance(existing, dict):
        raise ValueError("manual registration documents must be an object")
    documents = {str(key): dict(value) for key, value in existing.items()}
    added = []
    for raw in entries:
        entry = dict(raw)
        key = str(entry.get("manual_registration_key")
                  or registration_key(entry))
        if key in documents:
            continue
        documents[key] = {
            "run_id": entry.get("run_id"),
            "request_no": entry.get("request_no"),
            "dataset_epoch": entry.get("dataset_epoch"),
            "doc_no": entry.get("doc_no"),
            "owner": None,
            "decision": None,
            "external_check_result": None,
            "external_evidence": None,
            "notes": None,
        }
        added.append(key)
    legacy = {
        key: value for key, value in documents.items()
        if "::" not in key
    }
    payload = {
        **{key: value for key, value in existing_payload.items()
           if key not in {"schema_version", "documents"}},
        "schema_version": 2,
        "key_format": "dataset_epoch::request_no::run_id (legacy::run_id when unavailable)",
        "documents": documents,
    }
    if legacy:
        payload["migration_warning"] = (
            "Legacy doc_no-keyed rows are preserved but are not used for merging; "
            "copy human decisions into the matching stable-key rows after review.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return {"path": str(path.resolve()), "added": added,
            "legacy_keys": sorted(legacy), "total": len(documents)}


def merge_manual_registrations(entries: Iterable[dict],
                               registrations: Mapping[str, Mapping]) -> list[dict]:
    merged = []
    for evidence in entries:
        row = dict(evidence)
        key = str(row.get("manual_registration_key")
                  or registration_key(row))
        row["manual_registration_key"] = key
        manual = dict(registrations.get(key, {}))
        row["manual_registration"] = manual or None
        merged.append(row)
    return merged


def validate_ledger(entries: Iterable[dict], *, require_manual: bool = True) -> None:
    seen: dict[str, dict] = {}
    entries = list(entries)
    persisted_by_doc: dict[str, list[dict]] = {}
    for entry in entries:
        run_id = entry.get("run_id")
        if not run_id:
            raise ValueError("ledger entry missing run_id")
        if run_id in seen and seen[run_id] != entry:
            raise ValueError(f"conflicting ledger entries for run_id {run_id}")
        seen[run_id] = entry
        evidence = Path(str(entry.get("evidence_jsonl", "")))
        if not evidence.is_file():
            raise ValueError(f"ledger evidence does not exist: {evidence}")
        if entry.get("write_request_issued") and not entry.get("doc_no_preallocated"):
            raise ValueError(f"issued write lacks preallocated document number: {run_id}")
        if require_manual and entry.get("persisted") is True:
            manual = entry.get("manual_registration") or {}
            retired = (
                manual.get("decision") == "DELETE"
                and manual.get("external_check_result")
                    == "DELETED_CONFIRMED_BY_OPERATOR"
            ) or (
                manual.get("decision") == "VOID"
                and manual.get("external_check_result")
                    == "VOIDED_CONFIRMED_BY_OPERATOR"
            )
            if (entry.get("identity_strength")
                    != IdentityStrength.REQUEST_FIELD_MATCH.value
                    and not retired):
                raise ValueError(
                    "persisted document lacks stable request-field identity: "
                    f"{entry.get('doc_no')} ({run_id})")
            if not str(manual.get("owner") or "").strip():
                raise ValueError(
                    f"persisted document lacks manual owner: {entry.get('doc_no')}")
            if manual.get("decision") not in {"RETAIN", "VOID", "DELETE"}:
                raise ValueError(
                    f"persisted document lacks retain/void/delete decision: "
                    f"{entry.get('doc_no')}")
        if entry.get("persisted") is True and entry.get("doc_no"):
            persisted_by_doc.setdefault(str(entry["doc_no"]), []).append(entry)
    for doc_no, matches in persisted_by_doc.items():
        active = [row for row in matches if not (
            (row.get("manual_registration") or {}).get("decision")
            in {"DELETE", "VOID"}
            and (row.get("manual_registration") or {}).get(
                "external_check_result")
            in {"DELETED_CONFIRMED_BY_OPERATOR",
                "VOIDED_CONFIRMED_BY_OPERATOR"}
        )]
        keys = {str(row.get("document_identity_key") or "") for row in active}
        if len(active) > 1 and len(keys) > 1:
            raise ValueError(
                "duplicate document number has conflicting identities: "
                f"{doc_no}; runs={[row.get('run_id') for row in active]}")
