"""Fixed-argv child execution and evidence-based terminal classification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import (ArtifactError, ArtifactManifest,
                        load_and_verify_artifact)
from .models import JobStatus, TaskRisk
from .registry import WorkflowRegistry
from .reporting import ReceiverJournal
from .sqlite_queue import ClaimedJob, SQLiteJobQueue


@dataclass(frozen=True)
class EvidenceSummary:
    records: tuple[dict[str, Any], ...]
    terminals: tuple[dict[str, Any], ...]
    login_submit_count: int
    write_intent_count: int
    issued_write_count: int
    paths: tuple[str, ...]

    @property
    def dangerous_action_observed(self) -> bool:
        return bool(self.login_submit_count or self.write_intent_count
                    or self.issued_write_count)


@dataclass(frozen=True)
class ExecutionResult:
    status: JobStatus
    reason: str
    result: dict[str, Any]
    failure_code: str | None = None
    halt_receiver: bool = False


def scan_child_evidence(evidence_dir: Path) -> EvidenceSummary:
    child = Path(evidence_dir) / "child"
    records: list[dict[str, Any]] = []
    paths: list[str] = []
    if child.exists():
        for path in sorted(child.rglob("*.jsonl")):
            paths.append(str(path.resolve()))
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            # A running child can leave one partial final line.
                            continue
                        if isinstance(record, dict):
                            record["_source_path"] = str(path.resolve())
                            records.append(record)
            except OSError:
                continue
    terminals = tuple(row for row in records
                      if row.get("event") == "run_finished")
    login_submit = sum(row.get("event") == "login_submit" for row in records)
    write_intent = sum(row.get("event") == "write_intent" for row in records)
    issued_write = sum(
        row.get("event") == "step_act"
        and bool((row.get("details") or {}).get("write_request_issued"))
        for row in records)
    return EvidenceSummary(
        records=tuple(records), terminals=terminals,
        login_submit_count=login_submit,
        write_intent_count=write_intent,
        issued_write_count=issued_write, paths=tuple(paths))


def _classify_completed(claimed: ClaimedJob, spec, exit_code: int,
                        summary: EvidenceSummary) -> ExecutionResult:
    base = {
        "exit_code": exit_code,
        "terminal_count": len(summary.terminals),
        "login_submit_count": summary.login_submit_count,
        "write_intent_count": summary.write_intent_count,
        "issued_write_count": summary.issued_write_count,
        "child_evidence": list(summary.paths),
        "safe_to_retry": False,
    }
    if len(summary.terminals) != 1:
        if claimed.task.risk == TaskRisk.COMMIT and summary.dangerous_action_observed:
            return ExecutionResult(
                JobStatus.REQUIRES_HUMAN,
                "COMMIT 子进程结束但终态证据不唯一或缺失，且已有写动作证据",
                {**base, "safe_to_retry": False}, "TERMINAL_EVIDENCE_MISSING")
        if (claimed.task.risk == TaskRisk.REVERSIBLE_WRITE
                and summary.login_submit_count):
            return ExecutionResult(
                JobStatus.UNKNOWN,
                "登录已提交但缺少唯一终态证据",
                {**base, "safe_to_retry": False}, "LOGIN_RESULT_UNKNOWN")
        return ExecutionResult(
            JobStatus.FAILED, "子进程没有且仅有一个 run_finished",
            {**base, "safe_to_retry": claimed.task.risk == TaskRisk.READ_ONLY},
            "TERMINAL_EVIDENCE_MISSING")
    terminal = summary.terminals[0]
    terminal_status = str(terminal.get("status") or "")
    base.update({
        "child_terminal_status": terminal_status,
        "child_terminal_path": terminal.get("_source_path"),
        "child_safe_to_retry": terminal.get("safe_to_retry"),
    })
    if terminal_status in spec.success_statuses:
        if claimed.task.risk == TaskRisk.COMMIT:
            if summary.write_intent_count != 1 or summary.issued_write_count != 1:
                return ExecutionResult(
                    JobStatus.REQUIRES_HUMAN,
                    "COMMIT 成功态缺少唯一 write_intent/step_act 证据",
                    {**base, "safe_to_retry": False},
                    "COMMIT_CARDINALITY_INVALID")
            intent_index = next(
                i for i, row in enumerate(summary.records)
                if row.get("event") == "write_intent")
            act_index = next(
                i for i, row in enumerate(summary.records)
                if row.get("event") == "step_act"
                and bool((row.get("details") or {}).get("write_request_issued")))
            if intent_index >= act_index:
                return ExecutionResult(
                    JobStatus.REQUIRES_HUMAN,
                    "COMMIT write_intent 未严格早于写动作",
                    {**base, "safe_to_retry": False},
                    "COMMIT_EVIDENCE_ORDER_INVALID")
        if (claimed.task.workflow_id == "e10.session.login"
                and summary.login_submit_count != 1):
            return ExecutionResult(
                JobStatus.UNKNOWN, "登录成功态缺少唯一 login_submit 证据",
                {**base, "safe_to_retry": False}, "LOGIN_RESULT_UNKNOWN")
        return ExecutionResult(
            JobStatus.SUCCEEDED, "子 RPA 终态与白名单成功状态一致",
            {**base, "safe_to_retry": False})
    if claimed.task.risk == TaskRisk.COMMIT and summary.dangerous_action_observed:
        return ExecutionResult(
            JobStatus.REQUIRES_HUMAN,
            f"COMMIT 已有写证据但子终态为 {terminal_status}",
            {**base, "safe_to_retry": False}, "COMMIT_RESULT_UNCERTAIN")
    if (claimed.task.risk == TaskRisk.REVERSIBLE_WRITE
            and summary.login_submit_count):
        return ExecutionResult(
            JobStatus.UNKNOWN,
            f"登录已提交但子终态为 {terminal_status}",
            {**base, "safe_to_retry": False}, "LOGIN_RESULT_UNKNOWN")
    failure_code = str((terminal.get("details") or {}).get("failure_code") or
                       "CHILD_WORKFLOW_FAILED")
    safe_before_submit = (
        claimed.task.risk == TaskRisk.READ_ONLY
        or (claimed.task.risk == TaskRisk.REVERSIBLE_WRITE
            and summary.login_submit_count == 0
            and terminal.get("safe_to_retry") is True)
    )
    return ExecutionResult(
        JobStatus.FAILED, f"子 RPA 终态不属于成功集合: {terminal_status}",
        {**base, "safe_to_retry": safe_before_submit},
        failure_code)


def _write_result(evidence_dir: Path, execution: ExecutionResult) -> Path:
    path = Path(evidence_dir) / "result.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "status": execution.status.value,
        "reason": execution.reason,
        "failure_code": execution.failure_code,
        **execution.result,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def _stop_child(process: subprocess.Popen, timeout: float = 5) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def execute_claimed_job(queue: SQLiteJobQueue, claimed: ClaimedJob,
                        registry: WorkflowRegistry, *, attended: bool,
                        artifact_root: Path,
                        heartbeat_seconds: float = 2,
                        lease_seconds: float = 30,
                        popen_factory=subprocess.Popen) -> ExecutionResult:
    spec = registry.validate(claimed.task)
    evidence_dir = claimed.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    journal = ReceiverJournal(
        evidence_dir, job_id=claimed.task.job_id,
        worker_id=claimed.worker_id,
        lease_generation=claimed.lease_generation)
    artifact_path = None
    artifact_manifest_path = evidence_dir / "artifact_manifest.json"
    pending_artifact = (
        {
            "artifact_id": claimed.task.input.get("artifact_id"),
            "expected_sha256": claimed.task.input.get("sha256"),
            "verification": "PENDING",
        } if spec.artifact_extensions else None)
    artifact_manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "artifact": pending_artifact,
        "reason": ("awaiting receiver hash verification"
                   if pending_artifact else
                   "workflow does not accept an input artifact"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if queue.is_cancel_requested(claimed):
        execution = ExecutionResult(
            JobStatus.CANCELLED, "cancelled after lease and before child start",
            {"safe_to_retry": False, "child_started": False}, "CANCELLED")
        result_path = _write_result(evidence_dir, execution)
        journal.emit(
            "CANCELLED_BEFORE_START", result_path=str(result_path.resolve()))
        queue.cancel_unstarted(claimed, execution.reason)
        return execution
    if spec.artifact_extensions:
        try:
            manifest, artifact_path = load_and_verify_artifact(
                artifact_root, str(claimed.task.input["artifact_id"]),
                str(claimed.task.input["sha256"]), spec.artifact_extensions)
            shutil.copyfile(
                Path(artifact_root) / manifest.artifact_id / "artifact_manifest.json",
                artifact_manifest_path)
        except ArtifactError as exc:
            artifact_manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "artifact": pending_artifact,
                "verification": "REJECTED",
                "reason": str(exc),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            queue.mark_running(claimed, lease_seconds=lease_seconds)
            execution = ExecutionResult(
                JobStatus.FAILED, str(exc),
                {"safe_to_retry": False}, "ARTIFACT_INVALID")
            _write_result(evidence_dir, execution)
            queue.finish(
                claimed, execution.status, reason=execution.reason,
                result=execution.result, failure_code=execution.failure_code)
            return execution
    try:
        argv = registry.build_argv(
            claimed.task, evidence_dir=evidence_dir,
            artifact_path=artifact_path, attended=attended)
    except Exception as exc:
        queue.mark_running(claimed, lease_seconds=lease_seconds)
        execution = ExecutionResult(
            JobStatus.FAILED,
            f"fixed argv construction failed: {type(exc).__name__}: {exc}",
            {"safe_to_retry": False, "child_started": False},
            "ARGV_CONSTRUCTION_FAILED")
        _write_result(evidence_dir, execution)
        queue.finish(
            claimed, execution.status, reason=execution.reason,
            result=execution.result, failure_code=execution.failure_code)
        return execution
    queue.mark_running(claimed, lease_seconds=lease_seconds)
    journal.emit(
        "PROCESS_STARTING", workflow_id=claimed.task.workflow_id,
        risk=claimed.task.risk.value,
        executable_name=Path(argv[1]).name,
        artifact_id=claimed.task.input.get("artifact_id"))
    stdout_path = evidence_dir / "stdout.log"
    stderr_path = evidence_dir / "stderr.log"
    process = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, \
                stderr_path.open("w", encoding="utf-8") as stderr:
            environment = dict(os.environ)
            environment["PYTHONUNBUFFERED"] = "1"
            process = popen_factory(
                argv, shell=False, cwd=str(registry.root),
                stdout=stdout, stderr=stderr, text=True, env=environment)
            journal.emit("PROCESS_STARTED", process_id=process.pid)
            started = time.monotonic()
            next_heartbeat = started
            exceptional: ExecutionResult | None = None
            while process.poll() is None:
                now = time.monotonic()
                if now >= next_heartbeat:
                    queue.heartbeat(claimed, lease_seconds=lease_seconds)
                    journal.emit("HEARTBEAT", elapsed_seconds=round(now - started, 3))
                    next_heartbeat = now + heartbeat_seconds
                summary = scan_child_evidence(evidence_dir)
                cancel_requested = queue.is_cancel_requested(claimed)
                timed_out = now - started >= claimed.task.timeout_seconds
                if cancel_requested or timed_out:
                    trigger = "cancel_requested" if cancel_requested else "timeout"
                    if summary.dangerous_action_observed:
                        status = (JobStatus.REQUIRES_HUMAN
                                  if claimed.task.risk == TaskRisk.COMMIT
                                  else JobStatus.UNKNOWN)
                        exceptional = ExecutionResult(
                            status,
                            f"{trigger} occurred after dangerous action evidence; child not killed",
                            {
                                "safe_to_retry": False,
                                "child_process_id": process.pid,
                                "process_left_running": True,
                                "login_submit_count": summary.login_submit_count,
                                "write_intent_count": summary.write_intent_count,
                                "issued_write_count": summary.issued_write_count,
                            },
                            "CANCEL_OR_TIMEOUT_AFTER_SUBMIT",
                            halt_receiver=True)
                        break
                    _stop_child(process)
                    exceptional = ExecutionResult(
                        JobStatus.CANCELLED if cancel_requested else JobStatus.FAILED,
                        f"child stopped before dangerous action because of {trigger}",
                        {"safe_to_retry": claimed.task.risk == TaskRisk.READ_ONLY,
                         "process_left_running": False},
                        "CANCELLED" if cancel_requested else "TIMEOUT")
                    break
                time.sleep(0.1)
            if exceptional is not None:
                execution = exceptional
            else:
                exit_code = int(process.returncode)
                summary = scan_child_evidence(evidence_dir)
                execution = _classify_completed(
                    claimed, spec, exit_code, summary)
    except Exception as exc:
        summary = scan_child_evidence(evidence_dir)
        if process is not None and process.poll() is None:
            if summary.dangerous_action_observed:
                execution = ExecutionResult(
                    JobStatus.REQUIRES_HUMAN
                    if claimed.task.risk == TaskRisk.COMMIT else JobStatus.UNKNOWN,
                    f"receiver exception after submit: {type(exc).__name__}",
                    {"safe_to_retry": False,
                     "child_process_id": process.pid,
                     "process_left_running": True},
                    "RECEIVER_EXCEPTION_AFTER_SUBMIT", halt_receiver=True)
            else:
                _stop_child(process)
                execution = ExecutionResult(
                    JobStatus.FAILED,
                    f"receiver exception before submit: {type(exc).__name__}: {exc}",
                    {"safe_to_retry": claimed.task.risk == TaskRisk.READ_ONLY},
                    "RECEIVER_EXECUTION_ERROR")
        else:
            status = (JobStatus.REQUIRES_HUMAN
                      if claimed.task.risk == TaskRisk.COMMIT
                      and summary.dangerous_action_observed
                      else JobStatus.UNKNOWN
                      if claimed.task.risk == TaskRisk.REVERSIBLE_WRITE
                      and summary.login_submit_count
                      else JobStatus.FAILED)
            execution = ExecutionResult(
                status, f"receiver execution exception: {type(exc).__name__}: {exc}",
                {"safe_to_retry": status == JobStatus.FAILED
                 and claimed.task.risk == TaskRisk.READ_ONLY},
                "RECEIVER_EXECUTION_ERROR")
    result_path = _write_result(evidence_dir, execution)
    journal.emit(
        "PROCESS_CLASSIFIED", status=execution.status.value,
        reason=execution.reason, failure_code=execution.failure_code,
        result_path=str(result_path.resolve()))
    queue.finish(
        claimed, execution.status, reason=execution.reason,
        result={**execution.result,
                "result_path": str(result_path.resolve()),
                "receiver_journal": str(journal.path.resolve())},
        failure_code=execution.failure_code)
    if execution.halt_receiver:
        queue.update_worker(
            claimed.worker_id, status="REQUIRES_HUMAN",
            current_job_id=claimed.task.job_id)
    return execution


def recover_expired_jobs(queue: SQLiteJobQueue) -> list[dict[str, Any]]:
    outcomes = []
    for job in queue.expired_active_jobs():
        old_status = JobStatus(job["status"])
        risk = TaskRisk(job["risk"])
        evidence_dir = Path(job["evidence_dir"])
        summary = scan_child_evidence(evidence_dir)
        if old_status == JobStatus.LEASED:
            new_status = JobStatus.QUEUED
            reason = "expired lease before child was marked RUNNING"
        elif risk == TaskRisk.COMMIT:
            new_status = JobStatus.REQUIRES_HUMAN
            reason = "expired RUNNING COMMIT lease is never auto-requeued"
        elif risk == TaskRisk.REVERSIBLE_WRITE and summary.login_submit_count:
            new_status = JobStatus.UNKNOWN
            reason = "expired login lease after login_submit"
        elif job["attempt_count"] < job["max_attempts"]:
            new_status = JobStatus.QUEUED
            reason = "expired safe lease requeued within attempt limit"
        else:
            new_status = JobStatus.FAILED
            reason = "expired safe lease exhausted max_attempts"
        result = {
            "safe_to_retry": new_status == JobStatus.QUEUED,
            "login_submit_count": summary.login_submit_count,
            "write_intent_count": summary.write_intent_count,
            "issued_write_count": summary.issued_write_count,
        }
        queue.recover_expired(
            job["job_id"], int(job["lease_generation"]), new_status,
            reason=reason, result=result)
        outcomes.append({
            "job_id": job["job_id"], "old_status": old_status.value,
            "new_status": new_status.value, "reason": reason,
        })
    return outcomes
