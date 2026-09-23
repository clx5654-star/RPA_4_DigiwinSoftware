import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rpa_receiver
from rpa_control.executor import execute_claimed_job
from rpa_control.models import (SCHEMA_VERSION, JobStatus, TaskRequest,
                                TaskRisk, utc_now)
from rpa_control.registry import WorkflowSpec
from rpa_control.sqlite_queue import SQLiteJobQueue


FIXTURE = Path(__file__).parent / "fixtures" / "control_fake_workflow.py"


class FakeRegistry:
    def __init__(self, risk, mode, success_statuses):
        self.root = FIXTURE.parent.resolve()
        self.mode = mode
        self.spec = WorkflowSpec(
            workflow_id="test.fake", version="1", risk=risk,
            script_name=FIXTURE.name, required_inputs=frozenset(),
            success_statuses=frozenset(success_statuses), max_attempts=1,
            requires_attended=False, requires_interactive_desktop=False)

    def validate(self, task):
        if task.risk != self.spec.risk:
            raise ValueError("risk mismatch")
        return self.spec

    def build_argv(self, task, *, evidence_dir, artifact_path, attended):
        return [sys.executable, str(FIXTURE), "--report-dir",
                str(Path(evidence_dir) / "child"), "--mode", self.mode]


def fake_task(risk, job_id="JOB-FAKE"):
    return TaskRequest(
        schema_version=SCHEMA_VERSION, job_id=job_id,
        workflow_id="test.fake", workflow_version="1",
        environment_profile="E10_FT_TEST", risk=risk,
        request_no="REQ-FAKE" if risk == TaskRisk.COMMIT else None,
        idempotency_key=f"test.fake:{job_id}", input={},
        timeout_seconds=30, requested_by="test", created_at=utc_now())


class ControlExecutorTests(unittest.TestCase):
    def run_fake(self, risk, mode, success_statuses):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        queue = SQLiteJobQueue(root / "jobs.sqlite3")
        task = fake_task(risk)
        evidence = root / "evidence" / task.job_id
        queue.submit(task, max_attempts=1, evidence_dir=evidence)
        claimed = queue.claim_next("W1")
        result = execute_claimed_job(
            queue, claimed,
            FakeRegistry(risk, mode, success_statuses),
            attended=True, artifact_root=root / "input",
            heartbeat_seconds=0.05, lease_seconds=5)
        return result, queue.get_job(task.job_id), evidence

    def test_fake_workflow_full_success_closure(self):
        result, job, evidence = self.run_fake(
            TaskRisk.READ_ONLY, "success", {"SUCCESS"})
        self.assertEqual(JobStatus.SUCCEEDED, result.status)
        self.assertEqual(JobStatus.SUCCEEDED.value, job["status"])
        for name in ("stdout.log", "stderr.log", "receiver.jsonl", "result.json"):
            self.assertTrue((evidence / name).is_file(), name)
        self.assertTrue((evidence / "artifact_manifest.json").is_file())

    def test_exit_zero_without_run_finished_is_not_success(self):
        result, job, _ = self.run_fake(
            TaskRisk.READ_ONLY, "no_terminal", {"SUCCESS"})
        self.assertEqual(JobStatus.FAILED, result.status)
        self.assertNotEqual(JobStatus.SUCCEEDED.value, job["status"])

    def test_non_success_run_finished_is_failed(self):
        result, _, _ = self.run_fake(
            TaskRisk.READ_ONLY, "failed", {"SUCCESS"})
        self.assertEqual(JobStatus.FAILED, result.status)

    def test_login_submit_without_terminal_is_unknown(self):
        result, job, _ = self.run_fake(
            TaskRisk.REVERSIBLE_WRITE, "login_lost", {"AUTHENTICATED"})
        self.assertEqual(JobStatus.UNKNOWN, result.status)
        self.assertFalse(job["result"]["safe_to_retry"])

    def test_login_failure_before_submit_preserves_safe_retry_evidence(self):
        result, job, _ = self.run_fake(
            TaskRisk.REVERSIBLE_WRITE, "failed", {"AUTHENTICATED"})
        self.assertEqual(JobStatus.FAILED, result.status)
        self.assertEqual(0, job["result"]["login_submit_count"])
        self.assertTrue(job["result"]["safe_to_retry"])

    def test_commit_write_evidence_without_terminal_requires_human(self):
        result, job, _ = self.run_fake(
            TaskRisk.COMMIT, "commit_lost", {"SAVED_CONFIRMED"})
        self.assertEqual(JobStatus.REQUIRES_HUMAN, result.status)
        self.assertFalse(job["result"]["safe_to_retry"])

    def test_commit_success_requires_intent_then_single_act(self):
        result, job, _ = self.run_fake(
            TaskRisk.COMMIT, "commit_success", {"SAVED_CONFIRMED"})
        self.assertEqual(JobStatus.SUCCEEDED, result.status)
        self.assertEqual(1, job["result"]["write_intent_count"])
        self.assertEqual(1, job["result"]["issued_write_count"])

    def test_child_process_is_always_started_with_shell_false(self):
        captured = {}

        def factory(argv, **kwargs):
            captured["argv"] = argv
            captured["shell"] = kwargs.get("shell")
            return subprocess.Popen(argv, **kwargs)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        queue = SQLiteJobQueue(root / "jobs.sqlite3")
        task = fake_task(TaskRisk.READ_ONLY)
        queue.submit(task, max_attempts=1,
                     evidence_dir=root / "evidence" / task.job_id)
        claimed = queue.claim_next("W1")
        execute_claimed_job(
            queue, claimed,
            FakeRegistry(TaskRisk.READ_ONLY, "success", {"SUCCESS"}),
            attended=True, artifact_root=root / "input",
            popen_factory=factory)
        self.assertIsInstance(captured["argv"], list)
        self.assertIs(captured["shell"], False)

    def test_receiver_not_ready_does_not_claim_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = SQLiteJobQueue(root / "jobs.sqlite3")
            queue.register_worker(
                "W1", session_id=1, capabilities=[], status="STARTING")
            task = fake_task(TaskRisk.READ_ONLY)
            queue.submit(task, max_attempts=1,
                         evidence_dir=root / "evidence" / task.job_id)
            with mock.patch.object(
                    rpa_receiver, "_runtime_ready",
                    side_effect=RuntimeError("desktop unavailable")):
                result = rpa_receiver._run_one(
                    queue, FakeRegistry(TaskRisk.READ_ONLY, "success", {"SUCCESS"}),
                    worker_id="W1", attended=True,
                    artifact_root=root / "input")
            self.assertEqual("NOT_READY", result["status"])
            self.assertEqual(JobStatus.QUEUED.value,
                             queue.get_job(task.job_id)["status"])


if __name__ == "__main__":
    unittest.main()
