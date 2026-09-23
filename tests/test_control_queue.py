import json
import tempfile
import threading
import unittest
from pathlib import Path

from rpa_control.executor import recover_expired_jobs
from rpa_control.models import (SCHEMA_VERSION, JobStatus, TaskRequest,
                                TaskRisk, utc_now)
from rpa_control.sqlite_queue import QueueConflict, SQLiteJobQueue


def make_task(job_id="JOB-Q1", risk=TaskRisk.READ_ONLY,
              workflow="e10.requisition.verify"):
    request = "REQ-1" if risk == TaskRisk.COMMIT else None
    inputs = ({"artifact_id": "FILE-A", "sha256": "a" * 64}
              if risk == TaskRisk.COMMIT else {"doc_no": "3110-1"})
    return TaskRequest(
        schema_version=SCHEMA_VERSION, job_id=job_id,
        workflow_id=workflow, workflow_version="1",
        environment_profile="E10_FT_TEST", risk=risk,
        request_no=request,
        idempotency_key=(f"{workflow}:{request}" if request
                         else f"{workflow}:{job_id}"),
        input=inputs, timeout_seconds=30,
        requested_by="test", created_at=utc_now())


class ControlQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "state" / "jobs.sqlite3"
        self.queue = SQLiteJobQueue(self.db)

    def tearDown(self):
        self.temporary.cleanup()

    def submit(self, task=None, max_attempts=2):
        task = task or make_task()
        self.queue.submit(
            task, max_attempts=max_attempts,
            evidence_dir=self.root / "evidence" / task.job_id)
        return task

    def test_queued_job_survives_queue_reopen(self):
        task = self.submit()
        reopened = SQLiteJobQueue(self.db)
        self.assertEqual(JobStatus.QUEUED.value,
                         reopened.get_job(task.job_id)["status"])

    def test_two_workers_can_only_claim_same_job_once(self):
        self.submit()
        barrier = threading.Barrier(2)
        results = []

        def claim(worker):
            queue = SQLiteJobQueue(self.db)
            barrier.wait()
            results.append(queue.claim_next(worker))

        threads = [threading.Thread(target=claim, args=(f"W{i}",))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(item is not None for item in results))

    def test_global_ui_slot_blocks_second_job_claim(self):
        self.submit(make_task("JOB-Q1"))
        self.submit(make_task("JOB-Q2"))
        self.assertIsNotNone(self.queue.claim_next("W1"))
        self.assertIsNone(self.queue.claim_next("W2"))

    def test_fencing_token_rejects_old_worker_update(self):
        self.submit(max_attempts=2)
        first = self.queue.claim_next("W1")
        self.queue.release_unstarted(first, "test release")
        second = self.queue.claim_next("W2")
        self.assertGreater(second.lease_generation, first.lease_generation)
        with self.assertRaises(QueueConflict):
            self.queue.mark_running(first)
        self.queue.mark_running(second)

    def test_unstarted_lease_release_does_not_consume_attempt(self):
        task = self.submit(max_attempts=1)
        first = self.queue.claim_next("W1")
        self.queue.release_unstarted(first, "desktop changed")
        self.assertEqual(0, self.queue.get_job(task.job_id)["attempt_count"])
        self.assertIsNotNone(self.queue.claim_next("W2"))

    def test_queued_job_can_be_cancelled(self):
        task = self.submit()
        result = self.queue.cancel(task.job_id, requested_by="tester")
        self.assertEqual(JobStatus.CANCELLED.value, result["status"])

    def test_leased_cancel_is_honoured_before_child_start(self):
        task = self.submit()
        claimed = self.queue.claim_next("W1")
        self.queue.cancel(task.job_id, requested_by="tester")
        self.queue.cancel_unstarted(claimed, "receiver observed cancel")
        result = self.queue.get_job(task.job_id)
        self.assertEqual(JobStatus.CANCELLED.value, result["status"])
        self.assertFalse(result["result"]["child_started"])

    def test_running_commit_cancel_is_only_a_request(self):
        task = self.submit(
            make_task("JOB-C", TaskRisk.COMMIT,
                      "e10.requisition.create"), max_attempts=1)
        claimed = self.queue.claim_next("W1")
        self.queue.mark_running(claimed)
        result = self.queue.cancel(task.job_id, requested_by="tester")
        self.assertEqual(JobStatus.RUNNING.value, result["status"])
        self.assertTrue(result["cancel_requested"])

    def test_readonly_expired_running_lease_requeues_within_limit(self):
        task = self.submit(max_attempts=2)
        claimed = self.queue.claim_next("W1", lease_seconds=-1)
        self.queue.mark_running(claimed, lease_seconds=-1)
        outcomes = recover_expired_jobs(self.queue)
        self.assertEqual(JobStatus.QUEUED.value, outcomes[0]["new_status"])
        self.assertEqual(JobStatus.QUEUED.value,
                         self.queue.get_job(task.job_id)["status"])

    def test_commit_expired_running_lease_requires_human(self):
        task = self.submit(
            make_task("JOB-C", TaskRisk.COMMIT,
                      "e10.requisition.create"), max_attempts=1)
        claimed = self.queue.claim_next("W1", lease_seconds=-1)
        self.queue.mark_running(claimed, lease_seconds=-1)
        outcomes = recover_expired_jobs(self.queue)
        self.assertEqual(JobStatus.REQUIRES_HUMAN.value,
                         outcomes[0]["new_status"])
        self.assertEqual(JobStatus.REQUIRES_HUMAN.value,
                         self.queue.get_job(task.job_id)["status"])

    def test_login_submit_then_expired_lease_becomes_unknown(self):
        task = self.submit(make_task(
            "JOB-L", TaskRisk.REVERSIBLE_WRITE,
            "e10.session.login"), max_attempts=2)
        claimed = self.queue.claim_next("W1", lease_seconds=-1)
        self.queue.mark_running(claimed, lease_seconds=-1)
        child = claimed.evidence_dir / "child"
        child.mkdir(parents=True)
        (child / "login.jsonl").write_text(json.dumps({
            "event": "login_submit", "status": "SENT", "details": {}}) + "\n",
            encoding="utf-8")
        recover_expired_jobs(self.queue)
        self.assertEqual(JobStatus.UNKNOWN.value,
                         self.queue.get_job(task.job_id)["status"])

    def test_job_event_history_records_transitions(self):
        task = self.submit()
        claimed = self.queue.claim_next("W1")
        self.queue.mark_running(claimed)
        self.queue.finish(
            claimed, JobStatus.FAILED, reason="test",
            result={"safe_to_retry": True}, failure_code="FAKE")
        events = self.queue.events(task.job_id)
        self.assertEqual(["SUBMITTED", "CLAIMED", "STARTED", "FINISHED"],
                         [row["event"] for row in events])


if __name__ == "__main__":
    unittest.main()
