import time
import unittest

from rpa_core import ProgressHeartbeat


class HeartbeatTests(unittest.TestCase):
    def test_blocked_step_emits_liveness_without_business_success(self):
        output = []
        heartbeat = ProgressHeartbeat(interval=0.01, output=output.append)
        heartbeat.start()
        heartbeat.begin_step("select_item")
        time.sleep(0.04)
        heartbeat.stop()
        messages = "\n".join(output)
        self.assertIn("[HEARTBEAT] step=select_item", messages)
        self.assertIn("completion_evidence=none", messages)
        self.assertNotIn("CONFIRMED", messages)

    def test_stop_prevents_further_output(self):
        output = []
        heartbeat = ProgressHeartbeat(interval=0.01, output=output.append)
        heartbeat.start()
        heartbeat.begin_step("query")
        heartbeat.stop()
        count = len(output)
        time.sleep(0.03)
        self.assertEqual(count, len(output))


if __name__ == "__main__":
    unittest.main()
