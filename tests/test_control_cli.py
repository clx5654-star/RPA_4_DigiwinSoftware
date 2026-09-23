import json
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import rpa_receiver


ROOT = Path(__file__).resolve().parent.parent


class ControlCliTests(unittest.TestCase):
    def test_receiver_start_alias_is_foreground_serve_mode(self):
        args = rpa_receiver.build_parser().parse_args([
            "start", "--attended", "--poll-seconds", "1",
            "--status-seconds", "10",
        ])
        self.assertEqual("start", args.command)
        self.assertTrue(args.attended)
        self.assertEqual(1, args.poll_seconds)
        self.assertEqual(10, args.status_seconds)

    def test_receiver_serve_stays_until_ctrl_c_and_marks_stopped(self):
        queue = mock.Mock()
        queue.path = Path("test-receiver.sqlite3")
        output = io.StringIO()
        idle = {"status": "IDLE", "recoveries": []}
        with mock.patch.object(rpa_receiver, "SQLiteJobQueue",
                               return_value=queue), \
                mock.patch.object(rpa_receiver, "_run_one",
                                  return_value=idle), \
                mock.patch.object(rpa_receiver.time, "sleep",
                                  side_effect=KeyboardInterrupt), \
                redirect_stdout(output):
            exit_code = rpa_receiver.main([
                "serve", "--attended", "--poll-seconds", "0.2",
                "--status-seconds", "1",
            ])
        self.assertEqual(130, exit_code)
        queue.register_worker.assert_called_once()
        queue.update_worker.assert_called_with(
            mock.ANY, status="STOPPED")
        text = output.getvalue()
        self.assertIn("RECEIVER_STARTED", text)
        self.assertIn("RECEIVER_STATUS", text)
        self.assertIn("stopped", text)

    def test_receiver_explain_does_not_create_database_or_touch_e10(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "should-not-exist.sqlite3"
            result = subprocess.run(
                [sys.executable, str(ROOT / "rpa_receiver.py"),
                 "--db", str(db), "explain"],
                cwd=ROOT, capture_output=True, text=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual("EXPLAIN_ONLY", payload["mode"])
            self.assertFalse(payload["database_touched"])
            self.assertFalse(payload["e10_touched"])
            self.assertFalse(db.exists())

    def test_submit_and_status_persist_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "jobs.sqlite3"
            evidence = root / "evidence"
            base = [
                sys.executable, str(ROOT / "rpa_submit.py"),
                "--db", str(db), "--evidence-root", str(evidence),
            ]
            submitted = subprocess.run(
                [*base, "submit", "--workflow", "e10.session.login"],
                cwd=ROOT, capture_output=True, text=True, check=True)
            payload = json.loads(submitted.stdout)
            self.assertEqual("QUEUED", payload["status"])
            status = subprocess.run(
                [*base, "status", "--job-id", payload["job_id"]],
                cwd=ROOT, capture_output=True, text=True, check=True)
            saved = json.loads(status.stdout)
            self.assertEqual("QUEUED", saved["job"]["status"])
            raw = db.read_bytes()
            self.assertNotIn(b"HR12", raw)
            self.assertNotIn(b"password", raw.lower())


if __name__ == "__main__":
    unittest.main()
