"""Persist offline-only control-plane acceptance evidence under runs/."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rpa_control.executor import execute_claimed_job
from rpa_control.models import (SCHEMA_VERSION, TaskRequest, TaskRisk,
                                utc_now)
from rpa_control.registry import WorkflowSpec
from rpa_control.sqlite_queue import SQLiteJobQueue


FIXTURE = ROOT / "tests" / "fixtures" / "control_fake_workflow.py"


class AcceptanceRegistry:
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


def task(job_id, risk):
    return TaskRequest(
        schema_version=SCHEMA_VERSION, job_id=job_id,
        workflow_id="test.fake", workflow_version="1",
        environment_profile="E10_FT_TEST", risk=risk,
        request_no=f"REQ-{job_id}" if risk == TaskRisk.COMMIT else None,
        idempotency_key=f"test.fake:{job_id}", input={},
        timeout_seconds=30, requested_by="offline-acceptance",
        created_at=utc_now())


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "runs" / "control_plane_v1_offline" / stamp
    output.mkdir(parents=True, exist_ok=False)
    queue = SQLiteJobQueue(output / "control_jobs.sqlite3")
    scenarios = [
        ("READONLY_SUCCESS", TaskRisk.READ_ONLY, "success", {"SUCCESS"}),
        ("EXIT_ZERO_NO_TERMINAL", TaskRisk.READ_ONLY, "no_terminal", {"SUCCESS"}),
        ("LOGIN_SUBMIT_LOST", TaskRisk.REVERSIBLE_WRITE,
         "login_lost", {"AUTHENTICATED"}),
        ("COMMIT_ACT_LOST", TaskRisk.COMMIT,
         "commit_lost", {"SAVED_CONFIRMED"}),
    ]
    results = []
    for name, risk, mode, successes in scenarios:
        job_id = f"JOB-OFFLINE-{name}-{uuid4().hex[:6].upper()}"
        item = task(job_id, risk)
        evidence = output / "evidence" / job_id
        queue.submit(item, max_attempts=1, evidence_dir=evidence)
        claimed = queue.claim_next("OFFLINE-WORKER")
        execution = execute_claimed_job(
            queue, claimed, AcceptanceRegistry(risk, mode, successes),
            attended=True, artifact_root=output / "input")
        results.append({
            "scenario": name,
            "job_id": job_id,
            "status": execution.status.value,
            "evidence_dir": str(evidence.resolve()),
        })

    concurrency_job = task(
        f"JOB-OFFLINE-CONCURRENCY-{uuid4().hex[:6].upper()}",
        TaskRisk.READ_ONLY)
    queue.submit(
        concurrency_job, max_attempts=1,
        evidence_dir=output / "evidence" / concurrency_job.job_id)
    barrier = threading.Barrier(2)
    claims = []

    def claim(worker):
        local = SQLiteJobQueue(output / "control_jobs.sqlite3")
        barrier.wait()
        claims.append(local.claim_next(worker))

    threads = [threading.Thread(target=claim, args=(f"CW{i}",))
               for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winner = next(item for item in claims if item is not None)
    queue.release_unstarted(winner, "offline concurrency acceptance complete")

    reopened = SQLiteJobQueue(output / "control_jobs.sqlite3")
    summary = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "production_allowlist_modified": False,
        "e10_touched": False,
        "scenarios": results,
        "concurrency": {
            "worker_count": 2,
            "successful_claims": sum(item is not None for item in claims),
            "job_id": concurrency_job.job_id,
        },
        "persistence": {
            "database_reopened": True,
            "first_job_status": reopened.get_job(results[0]["job_id"])["status"],
        },
    }
    summary_path = output / "acceptance_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({
        "status": "OK", "output": str(output.resolve()),
        "summary": str(summary_path.resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
