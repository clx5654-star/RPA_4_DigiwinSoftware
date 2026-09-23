import unittest

from rpa_core import DesktopContext, evaluate_desktop_context


def _context(**changes):
    window = {"pid": 200, "hwnd": 300, "title": "鼎捷ERP E10 [FT]"}
    values = {
        "process_id": 100,
        "session_id": 2,
        "session_active": True,
        "window_station": "WinSta0",
        "thread_desktop": "Default",
        "input_desktop": "Default",
        "e10_process_sessions": ((200, 2),),
        "main_windows": (window,),
        "visible_e10_windows": (window,),
    }
    values.update(changes)
    return DesktopContext(**values)


class DesktopGuardTests(unittest.TestCase):
    def test_same_session_default_input_desktop_is_ready(self):
        decision = evaluate_desktop_context(_context())
        self.assertTrue(decision.allowed)
        self.assertEqual("READY", decision.code)

    def test_isolated_window_station_is_rejected(self):
        decision = evaluate_desktop_context(
            _context(window_station="Service-0x0-123$"))
        self.assertFalse(decision.allowed)
        self.assertEqual("INTERACTIVE_DESKTOP_UNAVAILABLE", decision.code)

    def test_locked_input_desktop_is_rejected(self):
        decision = evaluate_desktop_context(
            _context(input_desktop="Winlogon"))
        self.assertEqual("SESSION_LOCKED", decision.code)

    def test_e10_in_another_session_is_rejected(self):
        decision = evaluate_desktop_context(_context(
            e10_process_sessions=((200, 4),),
            main_windows=(), visible_e10_windows=()))
        self.assertEqual("DESKTOP_SESSION_MISMATCH", decision.code)

    def test_background_only_is_rejected_without_explicit_start(self):
        decision = evaluate_desktop_context(_context(
            main_windows=(), visible_e10_windows=()))
        self.assertEqual(
            "E10_WINDOW_NOT_VISIBLE_IN_CURRENT_DESKTOP", decision.code)

    def test_explicit_start_can_pass_only_after_desktop_is_proven(self):
        decision = evaluate_desktop_context(
            _context(main_windows=(), visible_e10_windows=()),
            allow_start_e10=True)
        self.assertTrue(decision.allowed)
        self.assertEqual("START_ALLOWED", decision.code)

    def test_multiple_main_windows_are_rejected(self):
        window = {"pid": 200, "hwnd": 300, "title": "鼎捷ERP E10 [FT]"}
        decision = evaluate_desktop_context(_context(
            main_windows=(window, {**window, "hwnd": 301})))
        self.assertEqual("WINDOW_AMBIGUOUS", decision.code)


if __name__ == "__main__":
    unittest.main()
