# -*- coding: utf-8 -*-
"""Interactive-session receiver for local E10 RPA control-plane jobs."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import sys
import time
from ctypes import wintypes
from pathlib import Path

from e10_credentials import CredentialStoreError, load_profile
from rpa_control.executor import execute_claimed_job, recover_expired_jobs
from rpa_control.receiver_control import (ReceiverCommand,
                                          ReceiverControlError,
                                          read_receiver_control)
from rpa_control.registry import WorkflowRegistry
from rpa_control.sqlite_queue import QueueConflict, SQLiteJobQueue


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "state" / "rpa_jobs.sqlite3"
DEFAULT_ARTIFACT_ROOT = ROOT / "jobs" / "input"
DEFAULT_CONTROL_FILE = ROOT / "state" / "receiver_control.json"
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_STATUS_SECONDS = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="E10 RPA 本机任务接收器（交互桌面前台常驻进程）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--artifact-root", type=Path,
                        default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--control-file", type=Path,
                        default=DEFAULT_CONTROL_FILE)
    parser.add_argument("--worker-id")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("explain", help="只显示能力，不创建数据库或触碰 E10")
    run_once = commands.add_parser(
        "run-once", help="领取并执行至多一个任务，然后退出")
    run_once.add_argument("--attended", action="store_true")
    serve = commands.add_parser(
        "serve", aliases=["start"],
        help="在当前 CMD/PowerShell 前台常驻并轮询任务（start 为别名）")
    serve.add_argument("--attended", action="store_true")
    serve.add_argument(
        "--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS,
        help="空闲轮询间隔，默认 2 秒")
    serve.add_argument(
        "--status-seconds", type=float, default=DEFAULT_STATUS_SECONDS,
        help="状态未变化时的控制台心跳间隔，默认 30 秒")
    return parser


def _session_id() -> int | None:
    value = wintypes.DWORD()
    if ctypes.windll.kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(value)):
        return int(value.value)
    return None


def _worker_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    return f"{socket.gethostname()}-S{_session_id()}-P{os.getpid()}"


def _runtime_ready(registry: WorkflowRegistry) -> dict:
    from e10_desktop_guard import require_interactive_e10
    from e10_query import get_digiwin_pids

    decision = require_interactive_e10(
        get_digiwin_pids(), allow_start_e10=True)
    environment = registry.environment("E10_FT_TEST")
    credential = load_profile(
        ROOT / ".secrets" / "credentials",
        environment.credential_profile)
    if credential.account_set != environment.login_account_set:
        raise CredentialStoreError(
            "凭据账套与 E10_FT_TEST 环境档案不一致")
    credential = None
    return {"desktop": decision.details,
            "credential_profile": environment.credential_profile}


def _run_one(queue: SQLiteJobQueue, registry: WorkflowRegistry, *,
             worker_id: str, attended: bool,
             artifact_root: Path) -> dict:
    recoveries = recover_expired_jobs(queue)
    try:
        ready = _runtime_ready(registry)
    except Exception as exc:
        queue.update_worker(worker_id, status="NOT_READY")
        return {
            "status": "NOT_READY", "message": str(exc),
            "failure_type": type(exc).__name__, "recoveries": recoveries,
        }
    if not attended:
        queue.update_worker(worker_id, status="NOT_READY")
        return {
            "status": "NOT_READY",
            "message": "当前白名单工作流均要求 receiver --attended",
            "recoveries": recoveries,
        }
    queue.update_worker(worker_id, status="READY")
    claimed = queue.claim_next(worker_id)
    if claimed is None:
        return {"status": "IDLE", "recoveries": recoveries,
                "runtime": ready}
    execution = execute_claimed_job(
        queue, claimed, registry, attended=attended,
        artifact_root=artifact_root)
    return {
        "status": execution.status.value,
        "job_id": claimed.task.job_id,
        "reason": execution.reason,
        "failure_code": execution.failure_code,
        "halt_receiver": execution.halt_receiver,
        "evidence_dir": str(claimed.evidence_dir.resolve()),
        "recoveries": recoveries,
    }


def _print_console_event(event: str, **details) -> None:
    """Emit one line so a foreground operator can see receiver liveness."""
    print(json.dumps({
        "event": event,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **details,
    }, ensure_ascii=False), flush=True)


def _serve_forever(queue: SQLiteJobQueue, registry: WorkflowRegistry, *,
                   worker_id: str, attended: bool, artifact_root: Path,
                   control_file: Path, poll_seconds: float,
                   status_seconds: float) -> int:
    """Run in the current console until Ctrl+C or a safety halt.

    IDLE/NOT_READY results are printed on change and periodically, rather than
    flooding the console on every poll. Job results are always printed.
    """
    poll_seconds = max(0.2, float(poll_seconds))
    status_seconds = max(poll_seconds, float(status_seconds))
    _print_console_event(
        "RECEIVER_STARTED",
        message=("接收器已在当前控制台常驻；空闲时按 Ctrl+C 正常停止，"
                 "任务运行中应先提交取消请求并等待终态"),
        worker_id=worker_id,
        process_id=os.getpid(),
        session_id=_session_id(),
        attended=attended,
        poll_seconds=poll_seconds,
        status_seconds=status_seconds,
        database=str(queue.path.resolve()),
        control_file=str(Path(control_file).resolve()),
    )
    last_signature = None
    last_output_at = 0.0
    while True:
        try:
            control = read_receiver_control(control_file)
        except ReceiverControlError as exc:
            queue.update_worker(worker_id, status="NOT_READY")
            result = {
                "status": "NOT_READY",
                "message": str(exc),
                "failure_type": type(exc).__name__,
                "recoveries": [],
            }
        else:
            if control.command == ReceiverCommand.STOP:
                queue.update_worker(worker_id, status="STOPPED")
                _print_console_event(
                    "RECEIVER_STOPPED",
                    message="已收到安全终止请求；当前没有运行中的子任务",
                    requested_by=control.requested_by,
                    requested_at=control.updated_at,
                    worker_id=worker_id,
                )
                return 0
            if control.command == ReceiverCommand.PAUSE:
                queue.update_worker(worker_id, status="PAUSED")
                result = {
                    "status": "PAUSED",
                    "message": "接收器已挂起，不领取新任务",
                    "requested_by": control.requested_by,
                    "requested_at": control.updated_at,
                    "recoveries": [],
                }
            else:
                result = _run_one(
                    queue, registry, worker_id=worker_id,
                    attended=attended, artifact_root=artifact_root)
        now = time.monotonic()
        status = str(result.get("status") or "UNKNOWN")
        signature = (
            status,
            result.get("message"),
            result.get("job_id"),
            result.get("failure_code"),
        )
        routine_status = status in {"IDLE", "NOT_READY", "PAUSED"}
        should_print = (
            not routine_status
            or signature != last_signature
            or now - last_output_at >= status_seconds
            or bool(result.get("recoveries"))
        )
        if should_print:
            _print_console_event("RECEIVER_STATUS", **result)
            last_output_at = now
        last_signature = signature
        if result.get("halt_receiver"):
            _print_console_event(
                "RECEIVER_HALTED",
                message="安全策略要求停止领取新任务，请人工处理当前任务",
                job_id=result.get("job_id"),
                status=status,
            )
            return 3
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = WorkflowRegistry()
    if args.command == "explain":
        print(json.dumps({
            "mode": "EXPLAIN_ONLY",
            "database_touched": False,
            "e10_touched": False,
            "environments": registry.explain_environments(),
            "workflows": registry.explain(),
        }, ensure_ascii=False, indent=2), flush=True)
        return 0

    worker_id = _worker_id(args.worker_id)
    queue = SQLiteJobQueue(args.db)
    queue.register_worker(
        worker_id, session_id=_session_id(),
        capabilities=[row["workflow_id"] for row in registry.explain()],
        status="STARTING")
    try:
        if args.command == "run-once":
            result = _run_one(
                queue, registry, worker_id=worker_id,
                attended=args.attended, artifact_root=args.artifact_root)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if result["status"] in {
                "IDLE", "SUCCEEDED"} else 2
        return _serve_forever(
            queue, registry, worker_id=worker_id,
            attended=args.attended, artifact_root=args.artifact_root,
            control_file=args.control_file,
            poll_seconds=args.poll_seconds,
            status_seconds=args.status_seconds)
    except KeyboardInterrupt:
        queue.update_worker(worker_id, status="STOPPED")
        print("\n[receiver] stopped", flush=True)
        return 130
    except (CredentialStoreError, QueueConflict, ValueError) as exc:
        queue.update_worker(worker_id, status="FAILED")
        print(json.dumps({
            "status": "FAILED", "error_type": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
