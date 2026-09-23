import json
import tempfile
import unittest
from pathlib import Path

from rpa_core import (derive_ledger, merge_manual_registrations,
                      scaffold_manual_registrations, validate_ledger)


class LedgerTests(unittest.TestCase):
    def test_read_only_verify_is_not_a_test_document_lifecycle_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "verify.jsonl"
            row = {
                "run_id": "verify-1",
                "timestamp": "2026-09-18T00:00:00+00:00",
                "workflow": {"id": "e10.requisition.verify", "version": "test"},
                "environment": {"account_set": "FRKTEST"},
                "event": "run_finished",
                "status": "NOT_APPLIED",
                "details": {"doc_no": "3110-NONE"},
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual([], derive_ledger(root))

    def test_preallocated_unsaved_and_issued_writes_are_derived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "run.jsonl"
            base = {
                "schema_version": 1,
                "run_id": "run-1",
                "timestamp": "2026-09-18T00:00:00+00:00",
                "workflow": {"id": "e10.requisition.create.end_to_end",
                             "version": "test"},
                "environment": {"account_set": "FRKTEST"},
            }
            rows = [
                {**base, "event": "run_started", "status": "RUNNING", "details": {}},
                {**base, "event": "write_intent", "status": "PENDING", "details": {
                    "idempotency_key": "req:1", "document_key": "3110-1",
                    "before_values": {"persisted": False,
                                      "doc_no_preallocated": "3110-1"}}},
                {**base, "event": "step_act", "status": "SENT", "details": {
                    "write_request_issued": True}},
                {**base, "event": "run_finished", "status": "FAILED", "details": {
                    "doc_no": "3110-1", "reconcile_status": "UNKNOWN"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                            encoding="utf-8")
            entries = derive_ledger(root)
            self.assertEqual(1, len(entries))
            self.assertEqual("3110-1", entries[0]["doc_no_preallocated"])
            self.assertTrue(entries[0]["write_request_issued"])
            self.assertIsNone(entries[0]["persisted"])
            validate_ledger(entries)

    def test_missing_preallocated_number_is_rejected_for_issued_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            entry = {
                "run_id": "run-2", "evidence_jsonl": str(path),
                "write_request_issued": True, "doc_no_preallocated": None,
            }
            with self.assertRaisesRegex(ValueError, "preallocated"):
                validate_ledger([entry])

    def test_persisted_document_requires_separate_manual_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.jsonl"
            evidence.write_text("{}\n", encoding="utf-8")
            entry = {
                "run_id": "saved-1", "evidence_jsonl": str(evidence),
                "write_request_issued": True, "doc_no_preallocated": "3110-1",
                "doc_no": "3110-1", "persisted": True,
                "dataset_epoch": "epoch-a", "request_no": "REQ-1",
                "document_identity_key": "epoch-a::REQ-1",
                "identity_strength": "REQUEST_FIELD_MATCH",
                "manual_registration_key": "epoch-a::REQ-1::saved-1",
            }
            with self.assertRaisesRegex(ValueError, "manual owner"):
                validate_ledger([entry])
            merged = merge_manual_registrations([entry], {
                "epoch-a::REQ-1::saved-1": {
                    "owner": "tester", "decision": "RETAIN"},
            })
            validate_ledger(merged)

    def test_same_doc_number_with_different_identities_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.jsonl"
            evidence.write_text("{}\n", encoding="utf-8")
            rows = []
            for run_id, request_no in (("run-a", "REQ-A"),
                                       ("run-b", "REQ-B")):
                rows.append({
                    "run_id": run_id, "evidence_jsonl": str(evidence),
                    "write_request_issued": True,
                    "doc_no_preallocated": "3110-3",
                    "doc_no": "3110-3", "persisted": True,
                    "dataset_epoch": "epoch-a", "request_no": request_no,
                    "document_identity_key": f"epoch-a::{request_no}",
                    "identity_strength": "REQUEST_FIELD_MATCH",
                    "manual_registration": {
                        "owner": "tester", "decision": "RETAIN"},
                })
            with self.assertRaisesRegex(ValueError, "conflicting identities"):
                validate_ledger(rows)

    def test_scaffold_uses_stable_keys_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.json"
            key = "epoch-a::REQ-1::run-1"
            path.write_text(json.dumps({
                "schema_version": 2,
                "documents": {key: {
                    "owner": "alice", "decision": "RETAIN"}},
            }), encoding="utf-8")
            entries = [
                {"run_id": "run-1", "dataset_epoch": "epoch-a",
                 "request_no": "REQ-1", "doc_no": "3110-1",
                 "manual_registration_key": key},
                {"run_id": "run-2", "dataset_epoch": "epoch-a",
                 "request_no": "REQ-2", "doc_no": "3110-2",
                 "manual_registration_key": "epoch-a::REQ-2::run-2"},
            ]
            result = scaffold_manual_registrations(path, entries)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("alice", payload["documents"][key]["owner"])
            self.assertEqual(["epoch-a::REQ-2::run-2"], result["added"])

    def test_operator_confirmed_deletion_closes_legacy_identity_debt(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.jsonl"
            evidence.write_text("{}\n", encoding="utf-8")
            rows = []
            for run_id in ("old-a", "old-b"):
                rows.append({
                    "run_id": run_id, "evidence_jsonl": str(evidence),
                    "write_request_issued": True,
                    "doc_no_preallocated": "3110-3",
                    "doc_no": "3110-3", "persisted": True,
                    "identity_strength": "DOC_NO_ONLY",
                    "document_identity_key": run_id,
                    "manual_registration": {
                        "owner": "human_operator", "decision": "DELETE",
                        "external_check_result":
                            "DELETED_CONFIRMED_BY_OPERATOR",
                        "external_evidence": "conversation:2026-09-18"},
                })
            validate_ledger(rows)


if __name__ == "__main__":
    unittest.main()
