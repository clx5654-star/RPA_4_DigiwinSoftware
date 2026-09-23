import unittest

from rpa_control.models import (SCHEMA_VERSION, JobStatus, TaskProtocolError,
                                TaskRequest, TaskRisk, utc_now,
                                validate_task_payload, validate_transition)
from rpa_control.registry import ENVIRONMENT_PROFILES, WorkflowRegistry


def payload(**updates):
    value = {
        "schema_version": SCHEMA_VERSION,
        "job_id": "JOB-TEST-1",
        "workflow_id": "e10.session.login",
        "workflow_version": "1",
        "environment_profile": "E10_FT_TEST",
        "risk": "REVERSIBLE_WRITE",
        "request_no": None,
        "idempotency_key": "e10.session.login:JOB-TEST-1",
        "input": {},
        "timeout_seconds": 30,
        "requested_by": "unit-test",
        "created_at": utc_now(),
    }
    value.update(updates)
    return value


class ControlModelTests(unittest.TestCase):
    def test_unknown_top_level_field_is_rejected(self):
        with self.assertRaises(TaskProtocolError):
            validate_task_payload({**payload(), "surprise": True})

    def test_forbidden_fields_are_rejected_recursively(self):
        for key in ("password", "password_env_value", "command", "shell",
                    "script_path", "executable", "raw_argv"):
            with self.subTest(key=key), self.assertRaises(TaskProtocolError):
                validate_task_payload(payload(input={key: "secret"}))

    def test_unknown_workflow_is_rejected(self):
        task = TaskRequest.from_dict(payload(workflow_id="e10.unknown"))
        with self.assertRaises(TaskProtocolError):
            WorkflowRegistry().validate(task)

    def test_risk_must_match_registry(self):
        task = TaskRequest.from_dict(payload(risk="READ_ONLY"))
        with self.assertRaises(TaskProtocolError):
            WorkflowRegistry().validate(task)

    def test_only_registered_environment_is_accepted(self):
        task = TaskRequest.from_dict(
            payload(environment_profile="E10_PRODUCTION"))
        with self.assertRaises(TaskProtocolError):
            WorkflowRegistry().validate(task)

    def test_login_and_business_environment_are_separate(self):
        profile = ENVIRONMENT_PROFILES["E10_FT_TEST"]
        self.assertEqual("FT", profile.login_account_set)
        self.assertEqual("FRKTEST", profile.requisition_business_environment)
        self.assertNotEqual(profile.login_account_set,
                            profile.requisition_business_environment)

    def test_terminal_status_cannot_restart(self):
        with self.assertRaises(TaskProtocolError):
            validate_transition(JobStatus.UNKNOWN, JobStatus.QUEUED)
        with self.assertRaises(TaskProtocolError):
            validate_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)

    def test_commit_idempotency_key_must_bind_request(self):
        task = TaskRequest.from_dict(payload(
            workflow_id="e10.requisition.create", risk="COMMIT",
            request_no="REQ-1", idempotency_key="wrong",
            input={"artifact_id": "FILE-A", "sha256": "a" * 64}))
        with self.assertRaises(TaskProtocolError):
            WorkflowRegistry().validate(task)


if __name__ == "__main__":
    unittest.main()
