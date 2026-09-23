import tempfile
import unittest
from pathlib import Path

from rpa_core import (GateAction, RequestRegistry, RequestStatus,
                      resolve_request_no)


class RequestRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = RequestRegistry(Path(self.temporary.name) / "requests.json")

    def tearDown(self):
        self.temporary.cleanup()

    def _append(self, status, fingerprint="fp", attempt=1):
        self.registry.append(
            request_no="REQ-1", campaign_id="C1",
            payload_fingerprint=fingerprint, status=status,
            run_id="run-1", attempt=attempt, doc_no="3110-1",
            evidence_jsonl="run-1.jsonl")

    def test_confirmed_same_payload_never_reaches_second_act(self):
        self._append(RequestStatus.CONFIRMED)
        decision = self.registry.decide("REQ-1", "fp", "C1")
        calls = {"act": 0}
        if decision.action == GateAction.ALLOW_WRITE:
            calls["act"] += 1
        self.assertEqual(GateAction.RETURN_CONFIRMED, decision.action)
        self.assertEqual(0, calls["act"])

    def test_same_request_different_payload_is_rejected(self):
        self._append(RequestStatus.CONFIRMED)
        self.assertEqual(
            GateAction.REJECT,
            self.registry.decide("REQ-1", "different", "C1").action)

    def test_pending_and_unknown_are_reconcile_only(self):
        for status in (RequestStatus.PENDING, RequestStatus.UNKNOWN):
            with self.subTest(status=status):
                path = Path(self.temporary.name) / f"{status.value}.json"
                registry = RequestRegistry(path)
                registry.append(
                    request_no="REQ", campaign_id="C", payload_fingerprint="fp",
                    status=status, run_id="r", attempt=1, doc_no="3110-1")
                self.assertEqual(
                    GateAction.RECONCILE_ONLY,
                    registry.decide("REQ", "fp", "C").action)

    def test_not_applied_requires_explicit_retry_and_new_attempt(self):
        self._append(RequestStatus.NOT_APPLIED)
        self.assertEqual(
            GateAction.REJECT,
            self.registry.decide("REQ-1", "fp", "C1").action)
        allowed = self.registry.decide(
            "REQ-1", "fp", "C1", retry_not_applied=True)
        self.assertEqual(GateAction.ALLOW_WRITE, allowed.action)
        self.assertEqual(2, allowed.attempt)

    def test_request_number_is_required_and_conflicts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须"):
            resolve_request_no(None, None)
        with self.assertRaisesRegex(ValueError, "冲突"):
            resolve_request_no("XLSX-1", "CLI-2")
        self.assertEqual("CLI-1", resolve_request_no(None, "CLI-1"))

    def test_same_number_is_allowed_in_a_new_campaign(self):
        self._append(RequestStatus.CONFIRMED)
        self.assertEqual(
            GateAction.ALLOW_WRITE,
            self.registry.decide("REQ-1", "fp", "C2").action)

    def test_dataset_epoch_partitions_reused_test_database_numbers(self):
        self.registry.append(
            request_no="REQ-1", campaign_id="C1", dataset_epoch="epoch-a",
            payload_fingerprint="fp", status=RequestStatus.CONFIRMED,
            run_id="run-a", attempt=1, doc_no="3110-3")
        self.assertEqual(
            GateAction.RETURN_CONFIRMED,
            self.registry.decide(
                "REQ-1", "fp", "C1", dataset_epoch="epoch-a").action)
        self.assertEqual(
            GateAction.ALLOW_WRITE,
            self.registry.decide(
                "REQ-1", "fp", "C1", dataset_epoch="epoch-b").action)


if __name__ == "__main__":
    unittest.main()
