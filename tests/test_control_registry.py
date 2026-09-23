import tempfile
import unittest
from pathlib import Path

from rpa_control.models import (SCHEMA_VERSION, TaskProtocolError,
                                TaskRequest, TaskRisk, utc_now)
from rpa_control.registry import PRODUCTION_WORKFLOWS, WorkflowRegistry


def task(workflow_id, risk, input_value, request_no=None):
    return TaskRequest(
        schema_version=SCHEMA_VERSION, job_id="JOB-X",
        workflow_id=workflow_id, workflow_version="1",
        environment_profile="E10_FT_TEST", risk=risk,
        request_no=request_no,
        idempotency_key=(f"{workflow_id}:{request_no}" if request_no
                         else f"{workflow_id}:JOB-X"),
        input=input_value, timeout_seconds=30,
        requested_by="test", created_at=utc_now())


class ControlRegistryTests(unittest.TestCase):
    def test_production_allowlist_has_only_three_reviewed_workflows(self):
        self.assertEqual({
            "e10.session.login", "e10.requisition.create",
            "e10.requisition.verify"}, set(PRODUCTION_WORKFLOWS))
        self.assertFalse(any("fake" in key for key in PRODUCTION_WORKFLOWS))

    def test_login_argv_is_fixed_and_uses_ft_profile(self):
        registry = WorkflowRegistry()
        value = task("e10.session.login", TaskRisk.REVERSIBLE_WRITE, {})
        with tempfile.TemporaryDirectory() as temporary:
            argv = registry.build_argv(
                value, evidence_dir=Path(temporary), artifact_path=None,
                attended=True)
        self.assertIn("e10_login.py", argv[1])
        self.assertIn("ft_test_hr12", argv)
        self.assertIn("FT", argv)
        self.assertNotIn("FRKTEST", argv)

    def test_create_argv_maps_environment_without_payload_override(self):
        registry = WorkflowRegistry()
        value = task(
            "e10.requisition.create", TaskRisk.COMMIT,
            {"artifact_id": "FILE-A", "sha256": "a" * 64}, "REQ-1")
        with tempfile.TemporaryDirectory() as temporary:
            argv = registry.build_argv(
                value, evidence_dir=Path(temporary),
                artifact_path=Path(temporary) / "a.xlsx", attended=True)
        self.assertIn("FRKTEST", argv)
        self.assertIn("FRKTEST-RESET-20260918", argv)
        self.assertNotIn("ft_test_hr12", argv)

    def test_task_cannot_supply_extra_argv_like_input(self):
        with self.assertRaises(TaskProtocolError):
            task(
                "e10.requisition.verify", TaskRisk.READ_ONLY,
                {"doc_no": "1", "raw_argv": ["--unsafe"]})

    def test_attended_is_receiver_policy_not_task_field(self):
        registry = WorkflowRegistry()
        value = task("e10.session.login", TaskRisk.REVERSIBLE_WRITE, {})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TaskProtocolError):
                registry.build_argv(
                    value, evidence_dir=Path(temporary),
                    artifact_path=None, attended=False)


if __name__ == "__main__":
    unittest.main()
