import unittest

from rpa_core import (ReconcileEvidence, ReconcileStatus, RiskLevel,
                      StepExecutor, StepSpec, StepStatus, WriteIntent,
                      auto_retry_allowed)


class FakeJournal:
    def __init__(self):
        self.events = []

    def emit(self, event, status, message, **details):
        self.events.append((event, status, message, details))


def _intent():
    return WriteIntent(
        request_no="BR-TEST-001",
        payload_fingerprint="payload:test",
        document_key="draft:new",
        action="save requisition",
        before_values={"document": None},
        payload={"item": "X"},
    )


class StepProtocolTests(unittest.TestCase):
    def test_write_intent_is_recorded_before_action(self):
        journal = FakeJournal()
        order = []

        def act(_ctx, _target):
            order.append("act")
            self.assertIn("write_intent", [x[0] for x in journal.events])

        step = StepSpec(
            "save", RiskLevel.REVERSIBLE_WRITE,
            precondition=lambda _ctx: None,
            locate=lambda _ctx: object(),
            act=act,
            readback=lambda _ctx, _target: "saved",
            expect=lambda _ctx, value: self.assertEqual("saved", value),
            reconcile=lambda _ctx: ReconcileEvidence(
                ReconcileStatus.CONFIRMED, "fake_db", True),
            writes_business_data=True,
            intent=_intent(),
        )
        result = StepExecutor(journal).execute(step, {})
        self.assertEqual(["act"], order)
        self.assertEqual(StepStatus.SUCCEEDED, result.status)

    def test_uncertain_commit_never_reclicks(self):
        journal = FakeJournal()
        calls = {"act": 0, "reconcile": 0}

        def act(_ctx, _target):
            calls["act"] += 1

        def reconcile(_ctx):
            calls["reconcile"] += 1
            return ReconcileEvidence(
                ReconcileStatus.UNKNOWN, "fake_db_timeout", True)

        step = StepSpec(
            "save", RiskLevel.COMMIT,
            precondition=lambda _ctx: None,
            locate=lambda _ctx: object(),
            act=act,
            readback=lambda _ctx, _target: (_ for _ in ()).throw(
                TimeoutError("confirmation timeout")),
            expect=lambda _ctx, _value: None,
            reconcile=reconcile,
            writes_business_data=True,
            intent=_intent(),
            human_confirmed=True,
            require_external_reconcile=True,
        )
        result = StepExecutor(journal).execute(step, {})
        self.assertEqual(1, calls["act"])
        self.assertEqual(1, calls["reconcile"])
        self.assertEqual(ReconcileStatus.UNKNOWN, result.reconcile_status)
        self.assertFalse(result.safe_to_retry)
        self.assertFalse(result.auto_retry_allowed)

    def test_exception_inside_write_act_is_conservatively_reconciled(self):
        calls = {"act": 0, "reconcile": 0}

        def act(_ctx, _target):
            calls["act"] += 1
            raise RuntimeError("provider failed after possible click dispatch")

        def reconcile(_ctx):
            calls["reconcile"] += 1
            return ReconcileEvidence(
                ReconcileStatus.UNKNOWN, "fake_unavailable", False)

        step = StepSpec(
            "save", RiskLevel.COMMIT,
            precondition=lambda _ctx: None,
            locate=lambda _ctx: object(),
            act=act,
            readback=lambda _ctx, _target: None,
            expect=lambda _ctx, _value: None,
            reconcile=reconcile,
            writes_business_data=True,
            intent=_intent(),
            human_confirmed=True,
        )
        result = StepExecutor(FakeJournal()).execute(step, {})
        self.assertEqual({"act": 1, "reconcile": 1}, calls)
        self.assertTrue(result.write_request_issued)
        self.assertEqual(ReconcileStatus.UNKNOWN, result.reconcile_status)
        self.assertFalse(result.safe_to_retry)
        self.assertFalse(result.auto_retry_allowed)

    def test_reconcile_has_all_three_states(self):
        expected = {
            ReconcileStatus.CONFIRMED: StepStatus.SUCCEEDED,
            ReconcileStatus.NOT_APPLIED: StepStatus.FAILED,
            ReconcileStatus.UNKNOWN: StepStatus.FAILED,
        }
        for reconcile_status, step_status in expected.items():
            with self.subTest(reconcile_status=reconcile_status):
                step = StepSpec(
                    "save", RiskLevel.REVERSIBLE_WRITE,
                    precondition=lambda _ctx: None,
                    locate=lambda _ctx: object(),
                    act=lambda _ctx, _target: None,
                    readback=lambda _ctx, _target: "ok",
                    expect=lambda _ctx, _value: None,
                    reconcile=lambda _ctx, state=reconcile_status: ReconcileEvidence(
                        state, "fake", True),
                    writes_business_data=True,
                    intent=_intent(),
                )
                result = StepExecutor(FakeJournal()).execute(step, {})
                self.assertEqual(step_status, result.status)
                self.assertEqual(reconcile_status, result.reconcile_status)

    def test_risk_retry_matrix(self):
        self.assertTrue(auto_retry_allowed(
            RiskLevel.READ_ONLY, write_request_issued=False))
        self.assertTrue(auto_retry_allowed(
            RiskLevel.REVERSIBLE_WRITE, write_request_issued=False))
        self.assertTrue(auto_retry_allowed(
            RiskLevel.REVERSIBLE_WRITE, write_request_issued=True,
            reconcile_status=ReconcileStatus.NOT_APPLIED))
        self.assertFalse(auto_retry_allowed(
            RiskLevel.REVERSIBLE_WRITE, write_request_issued=True,
            reconcile_status=ReconcileStatus.UNKNOWN))
        self.assertFalse(auto_retry_allowed(
            RiskLevel.COMMIT, write_request_issued=False))

    def test_external_confirmation_cannot_be_faked_by_ui_only(self):
        step = StepSpec(
            "save", RiskLevel.COMMIT,
            precondition=lambda _ctx: None,
            locate=lambda _ctx: object(),
            act=lambda _ctx, _target: None,
            readback=lambda _ctx, _target: "success toast",
            expect=lambda _ctx, _value: None,
            reconcile=lambda _ctx: ReconcileEvidence(
                ReconcileStatus.CONFIRMED, "e10_success_toast", False),
            writes_business_data=True,
            intent=_intent(),
            human_confirmed=True,
            require_external_reconcile=True,
        )
        result = StepExecutor(FakeJournal()).execute(step, {})
        self.assertEqual(ReconcileStatus.UNKNOWN, result.reconcile_status)
        self.assertFalse(result.safe_to_retry)


if __name__ == "__main__":
    unittest.main()
