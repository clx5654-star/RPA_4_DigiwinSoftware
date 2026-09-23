import json
import tempfile
import unittest
from pathlib import Path

from rpa_core import (
    AmbiguousSelector,
    GridObservation,
    GridSnapshot,
    QueryStatus,
    RiskLevel,
    RunJournal,
    SelectorNotFound,
    WindowIdentity,
    WindowOwnership,
    classify_query_result,
    require_unique,
    safe_to_retry_after_failure,
)


def snapshot(po=None, *, reported=None, extra=""):
    if po is None:
        return GridSnapshot.from_table((), (), cell_count=0, reported_row_count=reported)
    return GridSnapshot.from_table(
        ("单号", "供应商"),
        ((po, extra or "P4087"),),
        reported_row_count=1 if reported is None else reported,
    )


class QueryClassificationTests(unittest.TestCase):
    def test_found_requires_refresh_and_stability(self):
        before = snapshot("OLD")
        current = snapshot("3500-19080171")
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(current), GridObservation(current)),
            expected={"单号": "3500-19080171"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FOUND, outcome.status)
        self.assertEqual("3500-19080171", outcome.matched_rows[0]["单号"])

    def test_unchanged_old_grid_is_failed_even_if_it_contains_target(self):
        old = snapshot("3500-19080171")
        outcome = classify_query_result(
            baseline=old,
            observations=(GridObservation(old), GridObservation(old)),
            expected={"单号": "3500-19080171"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FAILED, outcome.status)
        self.assertEqual("STALE_GRID", outcome.code)

    def test_same_cell_count_but_changed_values_is_refresh(self):
        before = snapshot("OLD")
        current = snapshot("NEW")
        self.assertEqual(before.cell_count, current.cell_count)
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(current), GridObservation(current)),
            expected={"单号": "NEW"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FOUND, outcome.status)

    def test_explicit_refresh_evidence_allows_same_content(self):
        current = snapshot("3500-19080171", reported=1)
        outcome = classify_query_result(
            baseline=current,
            observations=(GridObservation(current),
                          GridObservation(current, refresh_evidence=True)),
            expected={"单号": "3500-19080171"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FOUND, outcome.status)

    def test_empty_requires_explicit_zero_signal(self):
        before = snapshot("OLD")
        empty = snapshot(None, reported=0)
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(empty), GridObservation(empty)),
            expected={"单号": "MISSING"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.EMPTY, outcome.status)

    def test_empty_without_zero_signal_is_failed(self):
        before = snapshot("OLD")
        empty = snapshot(None)
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(empty), GridObservation(empty)),
            expected={"单号": "MISSING"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FAILED, outcome.status)
        self.assertEqual("EMPTY_NOT_CONFIRMED", outcome.code)

    def test_unacknowledged_submit_is_failed(self):
        before = snapshot("OLD")
        current = snapshot("NEW")
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(current), GridObservation(current)),
            expected={"单号": "NEW"},
            submission_acknowledged=False,
        )
        self.assertEqual("SUBMIT_NOT_ACKNOWLEDGED", outcome.code)

    def test_truncated_page_is_not_found(self):
        before = snapshot("OLD")
        current = GridSnapshot.from_table(
            ("单号",),
            tuple((f"PO-{index:03d}",) for index in range(23)),
            reported_row_count=100,
        )
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(current), GridObservation(current)),
            expected={"单号": "PO-000"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FAILED, outcome.status)
        self.assertEqual("INCOMPLETE_GRID", outcome.code)
        self.assertEqual("INCOMPLETE", outcome.details["completeness"])
        self.assertEqual(23, outcome.details["observed_rows"])
        self.assertEqual(100, outcome.details["reported_row_count"])

    def test_completeness_unverified_is_marked(self):
        before = snapshot("OLD")
        current = GridSnapshot.from_table(("单号",), (("NEW",),))
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(current), GridObservation(current)),
            expected={"单号": "NEW"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FOUND, outcome.status)
        self.assertEqual("UNVERIFIED", outcome.details["completeness"])

    def test_matching_reported_count_is_verified(self):
        before = snapshot("OLD")
        current = snapshot("NEW", reported=1)
        outcome = classify_query_result(
            baseline=before,
            observations=(GridObservation(current), GridObservation(current)),
            expected={"单号": "NEW"},
            submission_acknowledged=True,
        )
        self.assertEqual(QueryStatus.FOUND, outcome.status)
        self.assertEqual("VERIFIED", outcome.details["completeness"])

    def test_readonly_failure_is_safe_to_retry(self):
        self.assertTrue(safe_to_retry_after_failure(
            RiskLevel.READ_ONLY,
            write_request_issued=True,
            result_unknown=True,
        ))


class WindowOwnershipTests(unittest.TestCase):
    def test_only_new_exact_handles_are_owned_and_cleaned(self):
        existing = WindowIdentity(1, 10)
        created = WindowIdentity(1, 20)
        unrelated = WindowIdentity(2, 30)
        tracker = WindowOwnership.start((existing,))
        self.assertEqual({created, unrelated}, tracker.observe((existing, created, unrelated)))
        self.assertEqual({created}, tracker.cleanup_targets((existing, created)))
        self.assertNotIn(existing, tracker.cleanup_targets((existing, created)))

    def test_existing_handle_cannot_be_claimed(self):
        existing = WindowIdentity(1, 10)
        tracker = WindowOwnership.start((existing,))
        with self.assertRaises(ValueError):
            tracker.claim(existing)


class SelectorTests(unittest.TestCase):
    def test_unique_candidate_is_returned(self):
        self.assertEqual("one", require_unique(["one"], "test"))

    def test_zero_candidate_fails(self):
        with self.assertRaises(SelectorNotFound):
            require_unique([], "test")

    def test_multiple_candidates_fail_with_evidence(self):
        with self.assertRaises(AmbiguousSelector) as ctx:
            require_unique([1, 2], "test", describe=lambda x: {"id": x})
        self.assertEqual(({"id": 1}, {"id": 2}), ctx.exception.candidates)


class ReportingTests(unittest.TestCase):
    def test_jsonl_contains_required_identity_and_is_serializable(self):
        with tempfile.TemporaryDirectory() as td:
            journal = RunJournal(
                td,
                workflow_id="purchase_order.query",
                workflow_version="1",
                selector_version="test",
                risk=RiskLevel.READ_ONLY,
                environment={"dpi": 96},
            )
            journal.emit("run_finished", "EMPTY", "no rows",
                         business_data_modified=False, safe_to_retry=True)
            records = [json.loads(line) for line in Path(journal.path).read_text(
                encoding="utf-8").splitlines()]
            self.assertEqual(2, len(records))
            self.assertEqual(journal.run_id, records[-1]["run_id"])
            self.assertEqual("read_only", records[-1]["risk"])
            self.assertEqual("test", records[-1]["selector_version"])
            self.assertFalse(records[-1]["business_data_modified"])
            self.assertTrue(records[-1]["safe_to_retry"])
            self.assertEqual(96, records[-1]["environment"]["dpi"])


class BoundaryTests(unittest.TestCase):
    def test_core_does_not_import_windows_or_ui_modules(self):
        import rpa_core.grid as grid
        import rpa_core.models as models

        source = Path(grid.__file__).read_text(encoding="utf-8") + Path(
            models.__file__).read_text(encoding="utf-8")
        for forbidden in ("pywinauto", "ctypes", "subprocess"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
