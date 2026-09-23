import json
import tempfile
import unittest
from pathlib import Path

from rpa_core import build_campaign_report


class CampaignReportTests(unittest.TestCase):
    def _write(self, root: Path, name: str, workflow: str, status: str,
               *, request_no=None, code_hash="hash-1", attest=True,
               initial_unknown=False, terminal_failure_code=None):
        base = {
            "schema_version": 2,
            "run_id": name,
            "timestamp": "2026-09-18T00:00:00+00:00",
            "workflow": {"id": workflow, "version": "test"},
            "environment": {
                "campaign_id": "C1", "code_hash": code_hash,
                "attest_no_intervention": attest,
                "scenario_id": "S1", "expected_result": "SUCCESS",
                "dataset_epoch": "epoch-a" if request_no else None,
                "request_no": request_no,
                "document_identity_key": (
                    f"epoch-a::{request_no}" if request_no else None),
                "identity_strength": (
                    "REQUEST_FIELD_MATCH" if request_no else None),
            },
        }
        rows = [{**base, "event": "run_started", "status": "RUNNING",
                 "details": {"initial_state": {"unknown_windows": (
                     [{"title": "错误"}] if initial_unknown else [])}}}]
        if request_no:
            rows.extend([
                {**base, "event": "write_intent", "status": "PENDING",
                 "details": {"request_no": request_no,
                             "payload_fingerprint": "fp",
                             "document_key": "3110-1"}},
                {**base, "event": "step_act", "status": "SENT",
                 "details": {"write_request_issued": True,
                             "step_elapsed_ms": 10}},
            ])
        rows.append({**base, "event": "run_finished", "status": status,
                     "details": {
                         "doc_no": "3110-1", "corrections": {},
                         "request_no": request_no,
                         "dataset_epoch": "epoch-a" if request_no else None,
                         "document_identity_key": (
                             f"epoch-a::{request_no}" if request_no else None),
                         "identity_strength": (
                             "REQUEST_FIELD_MATCH" if request_no else None),
                         "failure_code": terminal_failure_code,
                     }})
        path = root / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8")

    def test_report_separates_partitions_and_red_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "verify", "e10.requisition.verify", "CONFIRMED")
            self._write(root, "save", "e10.requisition.create.end_to_end",
                        "SAVED_CONFIRMED", request_no="REQ-1")
            report = build_campaign_report(root, "C1", manual_registrations={
                "epoch-a::REQ-1::save": {
                           "owner": "tester", "decision": "RETAIN",
                           "external_check_result": "MATCHED",
                           "external_evidence": "print.pdf"},
            }, enforce_quotas=False)
            self.assertEqual(1, report["partitions"]["verify"]["passed"])
            self.assertEqual(1, report["partitions"]["e2e"]["passed"])
            self.assertEqual(0, report["red_lines"]["false_success_count"])
            self.assertEqual(0, report["red_lines"]["duplicate_submission_count"])
            self.assertTrue(report["campaign_valid"])

    def test_code_change_or_unattested_sample_invalidates_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "one", "e10.requisition.verify", "CONFIRMED")
            self._write(root, "two", "e10.requisition.verify", "CONFIRMED",
                        code_hash="hash-2", attest=False)
            report = build_campaign_report(root, "C1", enforce_quotas=False)
            self.assertFalse(report["code_hash_consistent"])
            self.assertEqual(1, report["data_quality"]["sample_invalid_count"])
            self.assertFalse(report["campaign_valid"])

    def test_successful_run_reclassifies_failed_wait_as_recovered_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "save", "e10.requisition.create.end_to_end",
                        "SAVED_CONFIRMED", request_no="REQ-1")
            path = root / "save.jsonl"
            rows = [json.loads(line) for line in path.read_text(
                encoding="utf-8").splitlines()]
            rows.insert(-1, {
                **rows[0], "event": "wait_timing", "status": "FAILED",
                "message": "unique_materialized_named",
                "details": {"wait_elapsed_ms": 1000},
            })
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8")
            report = build_campaign_report(root, "C1", manual_registrations={
                "epoch-a::REQ-1::save": {
                           "owner": "tester", "decision": "RETAIN",
                           "external_check_result": "MATCHED",
                           "external_evidence": "print.pdf"},
            }, enforce_quotas=False)
            self.assertEqual(
                1, report["observation_histogram"]["WAIT_TIMEOUT_RECOVERED"])
            self.assertNotIn("INTERNAL_ERROR", report["failure_histogram"])

    def test_content_match_makes_cross_run_red_line_unprovable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "save", "e10.requisition.create.end_to_end",
                        "SAVED_CONFIRMED", request_no="REQ-1")
            path = root / "save.jsonl"
            rows = [json.loads(line) for line in path.read_text(
                encoding="utf-8").splitlines()]
            rows[0]["environment"]["identity_strength"] = "CONTENT_MATCH"
            rows[-1]["details"]["identity_strength"] = "CONTENT_MATCH"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                            encoding="utf-8")
            report = build_campaign_report(root, "C1", manual_registrations={
                "epoch-a::REQ-1::save": {
                    "owner": "tester", "decision": "RETAIN",
                    "external_check_result": "MATCHED",
                    "external_evidence": "print.pdf"}}, enforce_quotas=False)
            self.assertEqual(
                "UNPROVABLE",
                report["red_lines"]["cross_run_duplicate_persistence"][
                    "status"])
            self.assertFalse(report["campaign_valid"])

    def test_only_preexisting_popup_makes_sample_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root, "preexisting", "e10.requisition.verify", "FAILED",
                initial_unknown=True, terminal_failure_code="INTERNAL_ERROR")
            self._write(
                root, "runtime", "e10.requisition.verify", "FAILED",
                initial_unknown=False, terminal_failure_code="INTERNAL_ERROR")
            report = build_campaign_report(root, "C1", enforce_quotas=False)
            summaries = {row["run_id"]: row for row in report["runs"]}
            self.assertFalse(summaries["preexisting"]["sample_eligible"])
            self.assertEqual(
                "PREEXISTING_UNKNOWN_WINDOW",
                summaries["preexisting"]["failure_code"])
            self.assertTrue(summaries["runtime"]["sample_eligible"])
            self.assertEqual("INTERNAL_ERROR",
                             summaries["runtime"]["failure_code"])
            self.assertEqual(1, report["data_quality"]["scene_invalid_count"])

    def test_operator_confirmed_deletion_is_complete_lifecycle_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "save", "e10.requisition.create.end_to_end",
                        "SAVED_CONFIRMED", request_no="REQ-1")
            report = build_campaign_report(root, "C1", manual_registrations={
                "epoch-a::REQ-1::save": {
                    "owner": "human_operator", "decision": "DELETE",
                    "external_check_result":
                        "DELETED_CONFIRMED_BY_OPERATOR",
                    "external_evidence": "conversation:2026-09-18",
                }}, enforce_quotas=False)
            self.assertEqual(
                0, report["data_quality"][
                    "saved_manual_verification_incomplete_count"])


if __name__ == "__main__":
    unittest.main()
