import unittest

import e10_login
from rpa_core import (LoginObservation, LoginPhase,
                      classify_login_observation)


class LoginStateTests(unittest.TestCase):
    def test_cli_never_accepts_password_literal(self):
        option_strings = {
            option
            for action in e10_login.build_parser()._actions
            for option in action.option_strings}
        self.assertNotIn("--password", option_strings)
        self.assertIn("--password-env", option_strings)

    def test_default_credential_profile_and_account_policy_are_test_only(self):
        args = e10_login.build_parser().parse_args(["--execute-login-live"])
        self.assertEqual("ft_test_hr12", args.credential_profile)
        self.assertEqual("FT", e10_login.require_development_account_set("FT"))
        with self.assertRaises(ValueError):
            e10_login.require_development_account_set("FRKPROD")
        with self.assertRaises(ValueError):
            e10_login.require_development_account_set(None)

    def test_plan_has_exactly_one_submit_step(self):
        steps = [row["step"] for row in e10_login.semantic_plan()]
        self.assertEqual(1, steps.count("submit_once"))

    def test_live_discovered_login_automation_ids_are_semantic(self):
        automation_ids = e10_login.LOGIN["automation_ids"]
        self.assertEqual("txtUserName", automation_ids["username"])
        self.assertEqual("txtPwd", automation_ids["password"])
        self.assertEqual("btnOK", automation_ids["submit"])
        self.assertTrue(all(not value.isdigit()
                            for value in automation_ids.values() if value))

    def test_main_without_login_window_is_authenticated(self):
        decision = classify_login_observation(LoginObservation(
            process_count=1, main_window_count=1))
        self.assertEqual(LoginPhase.AUTHENTICATED, decision.phase)
        self.assertTrue(decision.success)

    def test_login_window_is_ready_for_input(self):
        decision = classify_login_observation(LoginObservation(
            process_count=1, login_window_count=1))
        self.assertEqual(LoginPhase.READY_FOR_INPUT, decision.phase)
        self.assertFalse(decision.terminal)

    def test_failure_popup_is_rejected_and_never_auto_retried(self):
        decision = classify_login_observation(LoginObservation(
            process_count=1, login_window_count=1,
            failure_titles=("错误",)))
        self.assertEqual(LoginPhase.REJECTED, decision.phase)
        self.assertTrue(decision.terminal)
        self.assertFalse(decision.auto_retry_allowed)

    def test_unknown_window_fails_closed(self):
        decision = classify_login_observation(LoginObservation(
            process_count=1, unknown_titles=("未识别弹窗",)))
        self.assertEqual(LoginPhase.UNKNOWN, decision.phase)
        self.assertFalse(decision.success)

    def test_duplicate_login_window_is_ambiguous(self):
        decision = classify_login_observation(LoginObservation(
            process_count=2, login_window_count=2))
        self.assertEqual(LoginPhase.UNKNOWN, decision.phase)

    def test_client_absent_can_be_started(self):
        decision = classify_login_observation(LoginObservation())
        self.assertEqual(LoginPhase.CLIENT_ABSENT, decision.phase)
        self.assertTrue(decision.auto_retry_allowed)

    def test_startup_wait_tolerates_known_transient_for_40_seconds(self):
        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        def observe():
            ready = clock.value >= 40
            observation = LoginObservation(
                process_count=1,
                login_window_count=1 if ready else 0,
                transient_titles=() if ready else ("LoadForm",))
            details = {
                "pids": [2], "login_windows": (
                    [{"hwnd": 3, "pid": 2, "title": "登录"}]
                    if ready else []),
                "main_windows": [], "failure_windows": [],
                "transient_windows": ([] if ready else [{
                    "hwnd": 1, "pid": 2, "title": "LoadForm",
                    "class_name": "StartupShell"}]),
                "unknown_windows": [],
            }
            return classify_login_observation(observation), details

        decision, details = e10_login._wait_for_login_or_main(
            120, observe=observe, monotonic=clock.monotonic,
            sleep=clock.sleep)
        self.assertEqual(LoginPhase.READY_FOR_INPUT, decision.phase)
        self.assertGreaterEqual(clock.value, 40)
        phases = [row["phase"]
                  for row in details["startup_wait_observations"]]
        self.assertIn(LoginPhase.STARTING.value, phases)
        self.assertEqual(LoginPhase.READY_FOR_INPUT.value, phases[-1])

    def test_unregistered_unknown_shell_still_fails_immediately(self):
        class Clock:
            value = 0.0

            def monotonic(self):
                return self.value

            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()

        def observe():
            observation = LoginObservation(
                process_count=1, unknown_titles=("永久未知窗口",))
            return classify_login_observation(observation), {
                "pids": [2], "login_windows": [], "main_windows": [],
                "failure_windows": [], "transient_windows": [],
                "unknown_windows": [{
                    "hwnd": 1, "pid": 2, "title": "永久未知窗口",
                    "class_name": "Unknown"}],
            }

        decision, details = e10_login._wait_for_login_or_main(
            120, observe=observe, monotonic=clock.monotonic,
            sleep=clock.sleep)
        self.assertEqual(LoginPhase.UNKNOWN, decision.phase)
        self.assertLess(clock.value, 1)
        self.assertEqual(1, len(details["startup_wait_observations"]))


if __name__ == "__main__":
    unittest.main()
