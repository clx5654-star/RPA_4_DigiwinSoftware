import unittest

from rpa_core import WindowRecoveryError, plan_window_recovery


def candidate(**changes):
    row = {
        "hwnd": 101,
        "pid": 42,
        "title": "维护请购单",
        "visible": True,
        "iconic": False,
        "foreground": True,
        "rect": (100, 100, 900, 700),
        "intersects_desktop": True,
    }
    row.update(changes)
    return row


class WindowRecoveryPolicyTests(unittest.TestCase):
    def test_minimized_window_is_restored_and_raised(self):
        plan = plan_window_recovery(
            [candidate(iconic=True, foreground=False)],
            title="维护请购单", expected_pid=42)
        self.assertEqual(("RESTORE", "BRING_TO_FRONT"), plan.actions)

    def test_covered_window_is_raised_without_permanent_topmost(self):
        plan = plan_window_recovery(
            [candidate(foreground=False)],
            title="维护请购单", expected_pid=42)
        self.assertEqual(("BRING_TO_FRONT",), plan.actions)

    def test_offscreen_window_is_moved_back(self):
        plan = plan_window_recovery(
            [candidate(rect=(-5000, 50, -4200, 650),
                       intersects_desktop=False)],
            title="维护请购单", expected_pid=42)
        self.assertEqual(("MOVE_TO_VISIBLE",), plan.actions)

    def test_wrong_process_is_ignored(self):
        plan = plan_window_recovery(
            [candidate(pid=99)], title="维护请购单", expected_pid=42)
        self.assertIsNone(plan)

    def test_duplicate_exact_candidates_are_rejected(self):
        with self.assertRaisesRegex(WindowRecoveryError, "命中多个"):
            plan_window_recovery(
                [candidate(), candidate(hwnd=102)],
                title="维护请购单", expected_pid=42)

    def test_zero_rectangle_ghost_is_rejected(self):
        with self.assertRaisesRegex(WindowRecoveryError, "幽灵窗口"):
            plan_window_recovery(
                [candidate(rect=(0, 0, 0, 0))],
                title="维护请购单", expected_pid=42)

    def test_hidden_window_is_rejected(self):
        with self.assertRaisesRegex(WindowRecoveryError, "隐藏"):
            plan_window_recovery(
                [candidate(visible=False)],
                title="维护请购单", expected_pid=42)


if __name__ == "__main__":
    unittest.main()
