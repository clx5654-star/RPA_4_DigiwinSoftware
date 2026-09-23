import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import rpa_receiver
import rpa_submit_gui
from rpa_control.receiver_control import (
    ReceiverCommand, ReceiverControlError, read_receiver_control,
    write_receiver_control)
from rpa_submit_gui import ReceiverController, app_paths


class ReceiverControlProtocolTests(unittest.TestCase):
    def test_missing_control_defaults_to_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = read_receiver_control(Path(temporary) / "missing.json")
        self.assertEqual(ReceiverCommand.RUN, state.command)

    def test_control_round_trip_uses_fixed_vocabulary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receiver_control.json"
            write_receiver_control(
                path, ReceiverCommand.PAUSE, requested_by="operator")
            state = read_receiver_control(path)
        self.assertEqual(ReceiverCommand.PAUSE, state.command)
        self.assertEqual("operator", state.requested_by)

    def test_malformed_control_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receiver_control.json"
            path.write_text('{"command":"RUN","shell":"cmd"}',
                            encoding="utf-8")
            with self.assertRaises(ReceiverControlError):
                read_receiver_control(path)

    def test_pause_never_claims_and_stop_exits_cleanly(self):
        queue = mock.Mock()
        queue.path = Path("queue.sqlite3")
        pause = mock.Mock(command=ReceiverCommand.PAUSE,
                          requested_by="operator", updated_at="now")
        stop = mock.Mock(command=ReceiverCommand.STOP,
                         requested_by="operator", updated_at="now")
        with mock.patch.object(
                rpa_receiver, "read_receiver_control",
                side_effect=[pause, stop]), \
                mock.patch.object(rpa_receiver, "_run_one") as run_one, \
                mock.patch.object(rpa_receiver.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            result = rpa_receiver._serve_forever(
                queue, mock.Mock(), worker_id="W1", attended=True,
                artifact_root=Path("input"), control_file=Path("control.json"),
                poll_seconds=0.2, status_seconds=1)
        self.assertEqual(0, result)
        run_one.assert_not_called()
        self.assertEqual(
            [mock.call("W1", status="PAUSED"),
             mock.call("W1", status="STOPPED")],
            queue.update_worker.call_args_list)


class ReceiverControllerTests(unittest.TestCase):
    def test_start_uses_only_fixed_receiver_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = app_paths(Path(temporary))
            paths.root.mkdir(parents=True, exist_ok=True)
            (paths.root / "rpa_receiver.py").write_text(
                "# fixed receiver", encoding="utf-8")
            queue = mock.Mock()
            queue.list_workers.return_value = []
            process = mock.Mock(pid=4321)
            popen = mock.Mock(return_value=process)
            controller = ReceiverController(
                paths, queue=queue, popen_factory=popen)
            result = controller.start_or_resume(requested_by="operator")
        self.assertEqual("STARTED", result["action"])
        argv = popen.call_args.args[0]
        self.assertIn("rpa_receiver.py", argv[1])
        self.assertIn("--control-file", argv)
        self.assertEqual(["start", "--attended"], argv[-2:])
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_existing_worker_is_resumed_without_second_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = app_paths(Path(temporary))
            queue = mock.Mock()
            queue.list_workers.return_value = [{
                "process_id": 123, "status": "PAUSED",
                "worker_id": "W1", "heartbeat_at": "now",
                "current_job_id": None,
            }]
            popen = mock.Mock()
            controller = ReceiverController(
                paths, queue=queue, popen_factory=popen)
            with mock.patch.object(rpa_submit_gui, "_process_alive",
                                   return_value=True):
                result = controller.start_or_resume(requested_by="operator")
        self.assertEqual("RESUME_REQUESTED", result["action"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
