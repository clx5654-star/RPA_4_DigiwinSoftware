"""SQLite WAL queue with atomic claims, leases and fencing tokens."""

from __future__ import annotations

import json
import getpass
import os
import sqlite3
import socket
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (JobStatus, TaskRequest, TaskRisk, TERMINAL_STATUSES,
                     TaskProtocolError, utc_now, validate_transition)


class QueueConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedJob:
    task: TaskRequest
    worker_id: str
    lease_generation: int
    attempt_count: int
    max_attempts: int
    evidence_dir: Path


def _future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class SQLiteJobQueue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    workflow_id TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    environment_profile TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    request_no TEXT,
                    idempotency_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    leased_by TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    failure_code TEXT,
                    evidence_dir TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
                    ON jobs(workflow_id, environment_profile, idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    timestamp TEXT NOT NULL,
                    worker_id TEXT,
                    lease_generation INTEGER,
                    event TEXT NOT NULL,
                    old_status TEXT,
                    new_status TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_events_job
                    ON job_events(job_id, event_id);
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    process_id INTEGER NOT NULL,
                    session_id INTEGER,
                    hostname TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    current_job_id TEXT
                );
            """)

    @staticmethod
    def _event(connection: sqlite3.Connection, *, job_id: str,
               event: str, old_status: str | None, new_status: str | None,
               worker_id: str | None = None,
               lease_generation: int | None = None,
               details: dict[str, Any] | None = None) -> None:
        connection.execute("""
            INSERT INTO job_events(
                job_id, timestamp, worker_id, lease_generation, event,
                old_status, new_status, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_id, utc_now(), worker_id, lease_generation, event,
            old_status, new_status,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        ))

    def submit(self, task: TaskRequest, *, max_attempts: int,
               evidence_dir: Path) -> None:
        if max_attempts < 1:
            raise TaskProtocolError("max_attempts 必须大于零")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""
                    INSERT INTO jobs(
                        job_id, schema_version, workflow_id, workflow_version,
                        environment_profile, risk, request_no, idempotency_key,
                        payload_json, status, max_attempts, created_at,
                        evidence_dir)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task.job_id, task.schema_version, task.workflow_id,
                    task.workflow_version, task.environment_profile,
                    task.risk.value, task.request_no, task.idempotency_key,
                    task.to_json(), JobStatus.QUEUED.value, max_attempts,
                    task.created_at, str(Path(evidence_dir).resolve()),
                ))
                self._event(
                    connection, job_id=task.job_id, event="SUBMITTED",
                    old_status=None, new_status=JobStatus.QUEUED.value,
                    details={
                        "reason": "validated task submitted",
                        "requested_by": task.requested_by,
                        "submitted_os_identity": getpass.getuser(),
                        "submitted_process_id": os.getpid(),
                        "submitted_hostname": socket.gethostname(),
                        "evidence_path": str(Path(evidence_dir).resolve()),
                    })
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise QueueConflict(
                "job_id 或 idempotency_key 已存在，拒绝重复提交") from exc

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        if result.get("result_json"):
            result["result"] = json.loads(result["result_json"])
        else:
            result["result"] = None
        result.pop("result_json", None)
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return None if row is None else self._row_to_dict(row)

    def list_jobs(self, status: JobStatus | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status=?"
            params.append(JobStatus(status).value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY event_id",
                (job_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def register_worker(self, worker_id: str, *, session_id: int | None,
                        capabilities: Iterable[str], status: str) -> None:
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.execute("""
                INSERT INTO workers(
                    worker_id, process_id, session_id, hostname,
                    capabilities_json, status, heartbeat_at, current_job_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(worker_id) DO UPDATE SET
                    process_id=excluded.process_id,
                    session_id=excluded.session_id,
                    hostname=excluded.hostname,
                    capabilities_json=excluded.capabilities_json,
                    status=excluded.status,
                    heartbeat_at=excluded.heartbeat_at,
                    current_job_id=NULL
            """, (
                worker_id, os.getpid(), session_id, socket.gethostname(),
                json.dumps(sorted(set(capabilities))), status, now,
            ))

    def update_worker(self, worker_id: str, *, status: str,
                      current_job_id: str | None = None) -> None:
        with closing(self._connect()) as connection:
            connection.execute("""
                UPDATE workers SET status=?, heartbeat_at=?, current_job_id=?
                WHERE worker_id=?
            """, (status, utc_now(), current_job_id, worker_id))

    def list_workers(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT worker_id, process_id, session_id, hostname,
                       capabilities_json, status, heartbeat_at, current_job_id
                FROM workers ORDER BY heartbeat_at DESC, worker_id
            """).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["capabilities"] = json.loads(
                item.pop("capabilities_json"))
            result.append(item)
        return result

    def claim_next(self, worker_id: str, *, lease_seconds: float = 30
                   ) -> ClaimedJob | None:
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
                (JobStatus.LEASED.value, JobStatus.RUNNING.value)).fetchone()[0]
            if active:
                connection.rollback()
                return None
            row = connection.execute("""
                SELECT * FROM jobs
                WHERE status=? AND cancel_requested=0
                  AND attempt_count < max_attempts
                ORDER BY created_at, job_id LIMIT 1
            """, (JobStatus.QUEUED.value,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            generation = int(row["lease_generation"]) + 1
            lease_expires_at = _future(lease_seconds)
            updated = connection.execute("""
                UPDATE jobs SET
                    status=?, leased_by=?, lease_generation=?,
                    lease_expires_at=?, heartbeat_at=?,
                    attempt_count=attempt_count+1
                WHERE job_id=? AND status=? AND lease_generation=?
            """, (
                JobStatus.LEASED.value, worker_id, generation,
                lease_expires_at, now, row["job_id"],
                JobStatus.QUEUED.value, row["lease_generation"],
            )).rowcount
            if updated != 1:
                connection.rollback()
                return None
            self._event(
                connection, job_id=row["job_id"], event="CLAIMED",
                old_status=JobStatus.QUEUED.value,
                new_status=JobStatus.LEASED.value, worker_id=worker_id,
                lease_generation=generation,
                details={
                    "reason": "atomic claim",
                    "lease_expires_at": lease_expires_at,
                    "evidence_path": row["evidence_dir"],
                })
            connection.execute("""
                UPDATE workers SET status='BUSY', heartbeat_at=?,
                    current_job_id=? WHERE worker_id=?
            """, (now, row["job_id"], worker_id))
            connection.commit()
            task = TaskRequest.from_dict(json.loads(row["payload_json"]))
            return ClaimedJob(
                task=task, worker_id=worker_id,
                lease_generation=generation,
                attempt_count=int(row["attempt_count"]) + 1,
                max_attempts=int(row["max_attempts"]),
                evidence_dir=Path(row["evidence_dir"]),
            )

    def _fenced_transition(self, claimed: ClaimedJob, *,
                           old: JobStatus, new: JobStatus, event: str,
                           reason: str, result: dict[str, Any] | None = None,
                           failure_code: str | None = None,
                           lease_seconds: float | None = None) -> None:
        validate_transition(old, new)
        assignments = ["status=?", "heartbeat_at=?"]
        values: list[Any] = [new.value, utc_now()]
        if new == JobStatus.RUNNING:
            assignments.append("started_at=COALESCE(started_at, ?)")
            values.append(utc_now())
        if new in TERMINAL_STATUSES:
            assignments.extend([
                "finished_at=?", "result_json=?", "failure_code=?",
                "lease_expires_at=NULL"])
            values.extend([
                utc_now(), json.dumps(result or {}, ensure_ascii=False),
                failure_code])
        elif new == JobStatus.QUEUED:
            assignments.extend([
                "leased_by=NULL", "lease_expires_at=NULL",
                "heartbeat_at=NULL"])
            if old == JobStatus.LEASED:
                # A lease released before RUNNING is not an execution attempt.
                assignments.append("attempt_count=MAX(attempt_count-1, 0)")
        elif lease_seconds is not None:
            assignments.append("lease_expires_at=?")
            values.append(_future(lease_seconds))
        values.extend([
            claimed.task.job_id, old.value, claimed.worker_id,
            claimed.lease_generation])
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(f"""
                UPDATE jobs SET {', '.join(assignments)}
                WHERE job_id=? AND status=? AND leased_by=?
                  AND lease_generation=?
            """, values).rowcount
            if updated != 1:
                connection.rollback()
                raise QueueConflict(
                    "fencing token 拒绝迟到或非所有者任务更新")
            self._event(
                connection, job_id=claimed.task.job_id, event=event,
                old_status=old.value, new_status=new.value,
                worker_id=claimed.worker_id,
                lease_generation=claimed.lease_generation,
                details={"reason": reason, "evidence_path": str(claimed.evidence_dir)})
            if new in TERMINAL_STATUSES or new == JobStatus.QUEUED:
                connection.execute("""
                    UPDATE workers SET status='READY', heartbeat_at=?,
                        current_job_id=NULL WHERE worker_id=?
                """, (utc_now(), claimed.worker_id))
            connection.commit()

    def mark_running(self, claimed: ClaimedJob,
                     lease_seconds: float = 30) -> None:
        self._fenced_transition(
            claimed, old=JobStatus.LEASED, new=JobStatus.RUNNING,
            event="STARTED", reason="child process is about to start",
            lease_seconds=lease_seconds)

    def finish(self, claimed: ClaimedJob, status: JobStatus, *, reason: str,
               result: dict[str, Any], failure_code: str | None = None) -> None:
        status = JobStatus(status)
        if status not in TERMINAL_STATUSES:
            raise TaskProtocolError(f"finish 不接受状态 {status.value}")
        self._fenced_transition(
            claimed, old=JobStatus.RUNNING, new=status,
            event="FINISHED", reason=reason, result=result,
            failure_code=failure_code)

    def heartbeat(self, claimed: ClaimedJob,
                  lease_seconds: float = 30) -> None:
        with closing(self._connect()) as connection:
            updated = connection.execute("""
                UPDATE jobs SET heartbeat_at=?, lease_expires_at=?
                WHERE job_id=? AND status=? AND leased_by=?
                  AND lease_generation=?
            """, (
                utc_now(), _future(lease_seconds), claimed.task.job_id,
                JobStatus.RUNNING.value, claimed.worker_id,
                claimed.lease_generation)).rowcount
            if updated != 1:
                raise QueueConflict("fencing token 拒绝 heartbeat")
            connection.execute("""
                UPDATE workers SET heartbeat_at=? WHERE worker_id=?
            """, (utc_now(), claimed.worker_id))

    def release_unstarted(self, claimed: ClaimedJob, reason: str) -> None:
        self._fenced_transition(
            claimed, old=JobStatus.LEASED, new=JobStatus.QUEUED,
            event="LEASE_RELEASED", reason=reason)

    def cancel_unstarted(self, claimed: ClaimedJob, reason: str) -> None:
        self._fenced_transition(
            claimed, old=JobStatus.LEASED, new=JobStatus.CANCELLED,
            event="CANCELLED", reason=reason,
            result={"safe_to_retry": False, "child_started": False},
            failure_code="CANCELLED")

    def cancel(self, job_id: str, *, requested_by: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise QueueConflict(f"任务不存在: {job_id}")
            status = JobStatus(row["status"])
            if status == JobStatus.QUEUED:
                connection.execute("""
                    UPDATE jobs SET status=?, cancel_requested=1,
                        finished_at=? WHERE job_id=? AND status=?
                """, (JobStatus.CANCELLED.value, utc_now(), job_id,
                      JobStatus.QUEUED.value))
                self._event(
                    connection, job_id=job_id, event="CANCELLED",
                    old_status=status.value,
                    new_status=JobStatus.CANCELLED.value,
                    details={
                        "reason": "cancelled while queued",
                        "requested_by": requested_by,
                        "evidence_path": row["evidence_dir"],
                    })
            elif status in {JobStatus.LEASED, JobStatus.RUNNING}:
                connection.execute(
                    "UPDATE jobs SET cancel_requested=1 WHERE job_id=?",
                    (job_id,))
                self._event(
                    connection, job_id=job_id, event="CANCEL_REQUESTED",
                    old_status=status.value, new_status=status.value,
                    worker_id=row["leased_by"],
                    lease_generation=row["lease_generation"],
                    details={"requested_by": requested_by,
                             "risk": row["risk"],
                             "reason": "cooperative cancellation requested",
                             "evidence_path": row["evidence_dir"]})
            else:
                connection.rollback()
                raise QueueConflict(f"终态任务不可取消: {status.value}")
            connection.commit()
        result = self.get_job(job_id)
        assert result is not None
        return result

    def is_cancel_requested(self, claimed: ClaimedJob) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT cancel_requested FROM jobs
                WHERE job_id=? AND leased_by=? AND lease_generation=?
            """, (claimed.task.job_id, claimed.worker_id,
                  claimed.lease_generation)).fetchone()
        if row is None:
            raise QueueConflict("fencing token 拒绝取消状态读取")
        return bool(row[0])

    def expired_active_jobs(self, now: str | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT * FROM jobs
                WHERE status IN (?, ?) AND lease_expires_at < ?
                ORDER BY created_at
            """, (JobStatus.LEASED.value, JobStatus.RUNNING.value, now)).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def recover_expired(self, job_id: str, lease_generation: int,
                        new_status: JobStatus, *, reason: str,
                        result: dict[str, Any] | None = None) -> None:
        new_status = JobStatus(new_status)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise QueueConflict(f"任务不存在: {job_id}")
            old = JobStatus(row["status"])
            if old not in {JobStatus.LEASED, JobStatus.RUNNING}:
                connection.rollback()
                raise QueueConflict("任务不再处于可恢复租约状态")
            if int(row["lease_generation"]) != int(lease_generation):
                connection.rollback()
                raise QueueConflict("fencing token 拒绝旧恢复操作")
            if not row["lease_expires_at"] or row["lease_expires_at"] >= utc_now():
                connection.rollback()
                raise QueueConflict("租约尚未过期")
            validate_transition(old, new_status)
            if new_status == JobStatus.QUEUED:
                decrement = (", attempt_count=MAX(attempt_count-1, 0)"
                             if old == JobStatus.LEASED else "")
                connection.execute(f"""
                    UPDATE jobs SET status=?, leased_by=NULL,
                        lease_expires_at=NULL, heartbeat_at=NULL {decrement}
                    WHERE job_id=? AND lease_generation=?
                """, (new_status.value, job_id, lease_generation))
            else:
                connection.execute("""
                    UPDATE jobs SET status=?, finished_at=?,
                        lease_expires_at=NULL, result_json=?
                    WHERE job_id=? AND lease_generation=?
                """, (new_status.value, utc_now(),
                      json.dumps(result or {}, ensure_ascii=False),
                      job_id, lease_generation))
            self._event(
                connection, job_id=job_id, event="LEASE_RECOVERED",
                old_status=old.value, new_status=new_status.value,
                worker_id=row["leased_by"],
                lease_generation=lease_generation,
                details={"reason": reason,
                         "evidence_path": row["evidence_dir"]})
            connection.execute("""
                UPDATE workers SET status='STALE', heartbeat_at=?,
                    current_job_id=NULL WHERE worker_id=?
            """, (utc_now(), row["leased_by"]))
            connection.commit()

    def worker(self, worker_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE worker_id=?",
                (worker_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["capabilities"] = json.loads(result.pop("capabilities_json"))
        return result
