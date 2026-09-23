# -*- coding: utf-8 -*-
"""Submit and inspect local allow-listed E10 RPA jobs."""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rpa_control.artifacts import ArtifactError, import_artifact
from rpa_control.models import (JobStatus, SCHEMA_VERSION, TaskProtocolError,
                                TaskRequest, utc_now)
from rpa_control.registry import WorkflowRegistry
from rpa_control.sqlite_queue import QueueConflict, SQLiteJobQueue


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "state" / "rpa_jobs.sqlite3"
DEFAULT_ARTIFACT_ROOT = ROOT / "jobs" / "input"
DEFAULT_EVIDENCE_ROOT = ROOT / "jobs" / "evidence"


def _job_id() -> str:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"JOB-{date}-{uuid4().hex[:12].upper()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E10 RPA 本机任务触发器")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--artifact-root", type=Path,
                        default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--evidence-root", type=Path,
                        default=DEFAULT_EVIDENCE_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="提交白名单工作流任务")
    submit.add_argument("--workflow", required=True)
    submit.add_argument("--environment", default="E10_FT_TEST")
    submit.add_argument("--request-no")
    submit.add_argument("--input-file", type=Path)
    submit.add_argument("--doc-no")
    submit.add_argument("--requested-by", default=getpass.getuser())
    submit.add_argument("--timeout-seconds", type=int)

    status = commands.add_parser("status", help="查询一个任务及事件")
    status.add_argument("--job-id", required=True)

    listing = commands.add_parser("list", help="列出任务")
    listing.add_argument("--status", choices=[item.value for item in JobStatus])
    listing.add_argument("--limit", type=int, default=100)

    cancel = commands.add_parser("cancel", help="请求取消任务")
    cancel.add_argument("--job-id", required=True)
    cancel.add_argument("--requested-by", default="local-user")
    return parser


def _default_timeout(workflow_id: str) -> int:
    return {
        "e10.session.login": 240,
        "e10.requisition.create": 900,
        "e10.requisition.verify": 600,
    }.get(workflow_id, 600)


def submit_job(args, *, registry: WorkflowRegistry,
               queue: SQLiteJobQueue) -> dict:
    spec = registry.get(args.workflow)
    environment = registry.environment(args.environment)
    if environment.name != "E10_FT_TEST":
        raise TaskProtocolError("本机 v1 只允许 E10_FT_TEST")
    job_id = _job_id()
    task_input: dict[str, str] = {}
    artifact = None
    if spec.artifact_extensions:
        if args.input_file is None:
            raise TaskProtocolError("该工作流必须提供 --input-file")
        artifact = import_artifact(
            args.input_file, args.artifact_root, spec.artifact_extensions)
        task_input.update({
            "artifact_id": artifact.artifact_id,
            "sha256": artifact.sha256,
        })
    elif args.input_file is not None:
        raise TaskProtocolError("该工作流不接受 --input-file")
    if spec.workflow_id == "e10.requisition.verify":
        if not args.doc_no:
            raise TaskProtocolError("核对任务必须提供 --doc-no")
        task_input["doc_no"] = args.doc_no
        if args.request_no:
            task_input["request_no"] = args.request_no
    elif args.doc_no:
        raise TaskProtocolError("该工作流不接受 --doc-no")
    if spec.risk.value == "COMMIT" and not args.request_no:
        raise TaskProtocolError("COMMIT 任务必须提供 --request-no")
    request_no = args.request_no if spec.risk.value == "COMMIT" else None
    idempotency_key = (
        f"{spec.workflow_id}:{request_no}" if request_no
        else f"{spec.workflow_id}:{job_id}")
    task = TaskRequest(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        workflow_id=spec.workflow_id,
        workflow_version=spec.version,
        environment_profile=environment.name,
        risk=spec.risk,
        request_no=request_no,
        idempotency_key=idempotency_key,
        input=task_input,
        timeout_seconds=(args.timeout_seconds
                         if args.timeout_seconds is not None
                         else _default_timeout(spec.workflow_id)),
        requested_by=args.requested_by,
        created_at=utc_now(),
    )
    registry.validate(task)
    evidence_dir = args.evidence_root / job_id
    try:
        queue.submit(task, max_attempts=spec.max_attempts,
                     evidence_dir=evidence_dir)
    except Exception:
        if artifact is not None:
            directory = Path(args.artifact_root).resolve() / artifact.artifact_id
            if directory.parent == Path(args.artifact_root).resolve():
                shutil.rmtree(directory, ignore_errors=True)
        raise
    return {
        "job_id": job_id,
        "status": JobStatus.QUEUED.value,
        "workflow_id": spec.workflow_id,
        "environment_profile": environment.name,
        "risk": spec.risk.value,
        "artifact_id": artifact.artifact_id if artifact else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = WorkflowRegistry()
    queue = SQLiteJobQueue(args.db)
    try:
        if args.command == "submit":
            result = submit_job(args, registry=registry, queue=queue)
        elif args.command == "status":
            job = queue.get_job(args.job_id)
            if job is None:
                raise QueueConflict(f"任务不存在: {args.job_id}")
            result = {"job": job, "events": queue.events(args.job_id)}
        elif args.command == "list":
            status = JobStatus(args.status) if args.status else None
            result = {"jobs": queue.list_jobs(status, args.limit)}
        else:
            result = queue.cancel(
                args.job_id, requested_by=args.requested_by)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (ArtifactError, QueueConflict, TaskProtocolError, ValueError) as exc:
        print(json.dumps({
            "status": "REJECTED", "error_type": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
