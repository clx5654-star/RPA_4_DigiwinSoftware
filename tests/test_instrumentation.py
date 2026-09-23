import json
import tempfile
import unittest
from pathlib import Path

from rpa_core import (FailureCode, ObservationCode, RunJournal, classify_failure,
                      compute_code_hash, validate_terminal_contract)


class InstrumentationTests(unittest.TestCase):
    def test_failure_taxonomy_is_closed_and_every_code_has_a_case(self):
        cases = {
            "nav.function.维护请购单 未实体化": FailureCode.NAV_FUNCTION_NOT_MATERIALIZED,
            "左树选择失败": FailureCode.NAV_TREE_SELECT_FAILED,
            "窗口找不到": FailureCode.WINDOW_NOT_FOUND,
            "window ambiguous": FailureCode.WINDOW_AMBIGUOUS,
            "隐藏壳仍存在": FailureCode.WINDOW_HIDDEN_SHELL,
            "shell not ready": FailureCode.SHELL_NOT_READY,
            "控件未命中": FailureCode.CONTROL_NOT_FOUND,
            "控件命中多个": FailureCode.CONTROL_AMBIGUOUS,
            "控件未实体化": FailureCode.CONTROL_NOT_MATERIALIZED,
            "editor_scope missing": FailureCode.EDITOR_SCOPE_MISSING,
            "编辑器不唯一": FailureCode.EDITOR_NOT_UNIQUE,
            "读回不一致": FailureCode.READBACK_MISMATCH,
            "状态无法解析": FailureCode.STATUS_TEXT_UNPARSED,
            "reconcile result unknown": FailureCode.RECONCILE_UNVERIFIED,
            "write confirmed not applied": FailureCode.WRITE_NOT_APPLIED,
            "已有目标窗口，拒绝执行": FailureCode.PREEXISTING_WINDOW_BLOCKED,
            "输入无效": FailureCode.INPUT_INVALID,
            "unexpected bug": FailureCode.INTERNAL_ERROR,
            "desktop session mismatch": FailureCode.DESKTOP_SESSION_MISMATCH,
            "interactive desktop unavailable": FailureCode.INTERACTIVE_DESKTOP_UNAVAILABLE,
            "session locked": FailureCode.SESSION_LOCKED,
            "e10 window not visible in current desktop": (
                FailureCode.E10_WINDOW_NOT_VISIBLE_IN_CURRENT_DESKTOP),
            "mutex namespace denied": FailureCode.MUTEX_NAMESPACE_DENIED,
            "已有另一个E10 RPA实例正在运行": FailureCode.MUTEX_ALREADY_HELD,
            "当前状态：未知窗口: '错误'": (
                FailureCode.PREEXISTING_UNKNOWN_WINDOW),
            "scene not conformant": FailureCode.SCENE_NOT_CONFORMANT,
            "login rejected": FailureCode.LOGIN_REJECTED,
            "login result unknown": FailureCode.LOGIN_RESULT_UNKNOWN,
            "client start failed": FailureCode.CLIENT_START_FAILED,
        }
        observed = {classify_failure(message) for message in cases}
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(expected, classify_failure(message))
        observed.add(classify_failure("cancel", interrupted=True))
        self.assertEqual(set(FailureCode), observed)

    def test_non_success_terminal_gets_valid_failure_code(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RunJournal(
                directory, workflow_id="test", workflow_version="1",
                selector_version="1", risk="read_only",
                environment={"code_hash": "abc"})
            journal.emit("run_finished", "FAILED", "window not found")
            rows = [json.loads(line) for line in journal.path.read_text(
                encoding="utf-8").splitlines()]
            terminal = validate_terminal_contract(rows)
            self.assertEqual("WINDOW_NOT_FOUND",
                             terminal["details"]["failure_code"])

    def test_missing_terminal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate_terminal_contract([{"event": "run_started"}])

    def test_failed_wait_uses_observation_code_not_terminal_failure_code(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RunJournal(
                directory, workflow_id="test", workflow_version="1",
                selector_version="1", risk="read_only")
            row = journal.emit(
                "wait_timing", "FAILED", "unique_materialized_named")
            self.assertNotIn("failure_code", row["details"])
            self.assertEqual(
                ObservationCode.WAIT_TIMEOUT.value,
                row["details"]["observation_code"])
            journal.emit("run_finished", "OK", "recovered")

    def test_code_hash_changes_with_source_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("x=1\n", encoding="utf-8")
            (root / "selectors.json").write_text("{}", encoding="utf-8")
            first, manifest = compute_code_hash(root)
            (root / "a.py").write_text("x=2\n", encoding="utf-8")
            second, _ = compute_code_hash(root)
            self.assertNotEqual(first, second)
            self.assertEqual(["a.py", "selectors.json"],
                             [row["path"] for row in manifest])


if __name__ == "__main__":
    unittest.main()
