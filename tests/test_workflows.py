import unittest

from rpa_core import (IdentityStrength, QueryOutcome, QueryStatus,
                      ReconcileEvidence, ReconcileStatus, RiskLevel,
                      enforce_identity_strength, execute_verify_protocol, get_workflow,
                      validate_commit_cardinality, validate_step_sequence,
                      validate_window_transitions, workflow_plan)


def _record(step, status="OK", event="step", **details):
    return {"event": event, "status": status,
            "details": {"step": step, **details}}


class WorkflowRegistryTests(unittest.TestCase):
    def test_end_to_end_plan_uses_verified_names_and_metadata(self):
        plan = workflow_plan("e10.requisition.create.end_to_end")
        names = [row["step"] for row in plan]
        self.assertIn("open_new", names)
        self.assertIn("save_requisition_once", names)
        self.assertNotIn("new_document", names)
        self.assertNotIn("save_once", names)
        save = next(row for row in plan if row["step"] == "save_requisition_once")
        self.assertEqual(RiskLevel.COMMIT.value, save["risk"])
        self.assertTrue(save["modifies_business_data"])
        self.assertFalse(save["retryable"])

    def test_warehouse_is_skipped_only_when_not_in_input(self):
        omitted = workflow_plan(
            "e10.requisition.create.end_to_end", warehouse_provided=False)
        warehouse = next(row for row in omitted if row["step"] == "verify_warehouse")
        self.assertEqual("SKIPPED", warehouse["status"])
        self.assertFalse(warehouse["required"])
        provided = workflow_plan(
            "e10.requisition.create.end_to_end", warehouse_provided=True)
        warehouse = next(row for row in provided if row["step"] == "verify_warehouse")
        self.assertEqual("PLANNED", warehouse["status"])
        self.assertTrue(warehouse["required"])

    def test_success_requires_conditional_steps_and_explicit_skip(self):
        plan = workflow_plan("e10.requisition.create.end_to_end")
        records = [_record(
            row["step"], "SKIPPED" if row["step"] == "verify_warehouse" else "OK")
            for row in plan]
        observed = validate_step_sequence(
            "e10.requisition.create.end_to_end", records, successful=True)
        self.assertEqual(tuple(row["step"] for row in plan), observed)
        records = [row for row in records
                   if row["details"]["step"] != "verify_warehouse"]
        with self.assertRaisesRegex(ValueError, "warehouse omission"):
            validate_step_sequence(
                "e10.requisition.create.end_to_end", records, successful=True)

    def test_resume_sequence_is_subsequence_not_global_prefix(self):
        records = [
            _record("resume_precondition"),
            _record("select_item"),
            _record("set_quantity"),
            _record("verify_warehouse", "SKIPPED"),
        ]
        validate_step_sequence(
            "e10.requisition.create.resume_fill", records, successful=True)

    def test_unknown_or_out_of_order_step_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not declared"):
            validate_step_sequence(
                "e10.requisition.verify", [_record("save_requisition_once")],
                successful=False)
        with self.assertRaisesRegex(ValueError, "order regression"):
            validate_step_sequence(
                "e10.requisition.verify",
                [_record("query_by_document_no"), _record("open_browse")],
                successful=False)

    def test_commit_cardinality_checks_actual_issued_request(self):
        rows = [
            _record("save_requisition_once", "PENDING", "write_intent"),
            _record("save_requisition_once", "SENT", "step_act",
                    write_request_issued=True),
        ]
        validate_commit_cardinality(rows, saved=True)
        with self.assertRaisesRegex(ValueError, "more than once"):
            validate_commit_cardinality(rows + [rows[-1]], saved=True)
        with self.assertRaisesRegex(ValueError, "must precede"):
            validate_commit_cardinality(list(reversed(rows)), saved=True)

    def test_window_metadata_is_enforced_without_cleanup_equality(self):
        validate_window_transitions(
            "e10.requisition.verify",
            [{"action": "opened", "title": "浏览 - 维护请购单"},
             {"action": "closed", "title": "浏览 - 维护请购单"}],
            remaining_owned_windows=())
        with self.assertRaisesRegex(ValueError, "undeclared"):
            validate_window_transitions(
                "e10.requisition.verify",
                [{"action": "opened", "title": "错误"}])
        with self.assertRaisesRegex(ValueError, "remain"):
            validate_window_transitions(
                "e10.requisition.verify", [],
                remaining_owned_windows=("浏览 - 维护请购单",))


class FakeVerifyActor:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.calls = []

    def open(self):
        self.calls.append("open")

    def query(self, document_no):
        self.calls.append(("query", document_no))
        if self.error:
            raise self.error
        return self.outcome

    def cleanup(self):
        self.calls.append("cleanup")

    def create(self):
        raise AssertionError("verify must never create")

    def save(self):
        raise AssertionError("verify must never save")


class VerifyProtocolTests(unittest.TestCase):
    def test_verify_only_opens_queries_and_cleans_up(self):
        actor = FakeVerifyActor(QueryOutcome(
            QueryStatus.FOUND, "MATCHED", "ok", ({"单号": "3110-X"},)))
        result = execute_verify_protocol(actor, "3110-X")
        self.assertEqual(ReconcileStatus.CONFIRMED, result.status)
        self.assertEqual(["open", ("query", "3110-X"), "cleanup"], actor.calls)
        self.assertFalse(result.external)

    def test_empty_maps_to_not_applied(self):
        actor = FakeVerifyActor(QueryOutcome(
            QueryStatus.EMPTY, "ZERO_ROWS", "zero", details={
                "status_texts": ("(共0笔)",), "reported_row_count": 0}))
        result = execute_verify_protocol(actor, "3110-NONE")
        self.assertEqual(ReconcileStatus.NOT_APPLIED, result.status)
        self.assertEqual(("(共0笔)",), result.details["status_texts"])

    def test_failed_or_exception_maps_to_unknown(self):
        failed = FakeVerifyActor(QueryOutcome(
            QueryStatus.FAILED, "EMPTY_NOT_CONFIRMED", "unknown"))
        self.assertEqual(
            ReconcileStatus.UNKNOWN,
            execute_verify_protocol(failed, "3110-NONE").status)
        broken = FakeVerifyActor(error=TimeoutError("UIA stalled"))
        result = execute_verify_protocol(broken, "3110-X")
        self.assertEqual(ReconcileStatus.UNKNOWN, result.status)
        self.assertEqual("cleanup", broken.calls[-1])

    def test_verify_registry_contains_no_write_step(self):
        workflow = get_workflow("e10.requisition.verify")
        self.assertFalse(any(step.modifies_business_data for step in workflow.steps))
        self.assertNotIn("save_requisition_once", [step.name for step in workflow.steps])

    def test_bare_document_number_cannot_confirm_run_identity(self):
        evidence = ReconcileEvidence(
            ReconcileStatus.CONFIRMED, "E10_UI_TEST_DATABASE", False,
            {"doc_no": "3110-3"})
        downgraded = enforce_identity_strength(
            evidence, IdentityStrength.DOC_NO_ONLY)
        self.assertEqual(ReconcileStatus.UNKNOWN, downgraded.status)
        strong = enforce_identity_strength(
            evidence, IdentityStrength.REQUEST_FIELD_MATCH)
        self.assertEqual(ReconcileStatus.CONFIRMED, strong.status)


if __name__ == "__main__":
    unittest.main()
