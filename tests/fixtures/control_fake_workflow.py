"""Offline-only fake child process for control-plane executor tests."""

import argparse
import json
from pathlib import Path


def emit(path, event, status, **details):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "schema_version": 2,
            "workflow": {"id": "test.fake", "version": "1"},
            "risk": details.pop("risk", "read_only"),
            "event": event,
            "status": status,
            "safe_to_retry": details.pop("safe_to_retry", None),
            "details": details,
        }) + "\n")


parser = argparse.ArgumentParser()
parser.add_argument("--report-dir", type=Path, required=True)
parser.add_argument("--mode", required=True)
args = parser.parse_args()
args.report_dir.mkdir(parents=True, exist_ok=True)
journal = args.report_dir / "fake.jsonl"
emit(journal, "run_started", "RUNNING")

if args.mode == "success":
    emit(journal, "run_finished", "SUCCESS", safe_to_retry=False)
elif args.mode == "failed":
    emit(journal, "run_finished", "FAILED", failure_code="FAKE_FAILED",
         safe_to_retry=True)
elif args.mode == "login_lost":
    emit(journal, "login_submit", "SENT", safe_to_retry=False)
elif args.mode == "commit_lost":
    emit(journal, "write_intent", "PENDING", risk="commit")
    emit(journal, "step_act", "SENT", risk="commit",
         write_request_issued=True)
elif args.mode == "commit_success":
    emit(journal, "write_intent", "PENDING", risk="commit")
    emit(journal, "step_act", "SENT", risk="commit",
         write_request_issued=True)
    emit(journal, "run_finished", "SAVED_CONFIRMED", risk="commit",
         safe_to_retry=False)
# no_terminal intentionally leaves only run_started.
