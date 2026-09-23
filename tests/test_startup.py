import json
import tempfile
import unittest
from pathlib import Path

from rpa_core import OrphanAction, audit_orphaned_runs


class StartupAuditTests(unittest.TestCase):
    def _orphan(self, root: Path, run_id: str = "run-1") -> Path:
        row = {
            "run_id": run_id,
            "workflow": {"id": "e10.requisition.create.end_to_end"},
            "environment": {
                "request_no": "REQ-1", "dataset_epoch": "epoch-a",
                "payload_fingerprint": "fp"},
            "event": "run_started", "status": "RUNNING", "details": {},
        }
        path = root / f"{run_id}.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return path

    def test_pending_or_unknown_orphan_requires_reconcile_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._orphan(root)
            result = audit_orphaned_runs(
                root, request_no="REQ-1", dataset_epoch="epoch-a",
                registry_entries=[{"run_id": "run-1", "status": "PENDING"}])
            self.assertEqual(
                OrphanAction.RECONCILE_ONLY.value, result[0]["action"])

    def test_no_registry_orphan_requires_presave_draft_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._orphan(root)
            result = audit_orphaned_runs(
                root, request_no="REQ-1", dataset_epoch="epoch-a",
                registry_entries=[])
            self.assertEqual(
                OrphanAction.PRE_SAVE_DRAFT_CHECK_REQUIRED.value,
                result[0]["action"])

    def test_resolution_event_suppresses_resolved_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._orphan(root)
            row = {
                "run_id": "guard-1", "workflow": {
                    "id": "e10.requisition.orphan_guard"},
                "environment": {}, "event": "orphan_resolution", "status": "OK",
                "details": {"resolved_orphan_run_ids": ["run-1"]},
            }
            (root / "guard.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual([], audit_orphaned_runs(
                root, request_no="REQ-1", dataset_epoch="epoch-a",
                registry_entries=[]))


if __name__ == "__main__":
    unittest.main()
