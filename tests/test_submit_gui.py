import tempfile
import unittest
from pathlib import Path

from rpa_submit_gui import (ENVIRONMENT, SubmissionForm, app_paths,
                            build_submit_args)


class SubmitGuiTests(unittest.TestCase):
    def test_login_builds_empty_business_input_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = app_paths(Path(temporary))
            args = build_submit_args(
                SubmissionForm(
                    workflow_id="e10.session.login",
                    requested_by="operator"),
                paths)
        self.assertEqual("e10.session.login", args.workflow)
        self.assertEqual(ENVIRONMENT, args.environment)
        self.assertIsNone(args.request_no)
        self.assertIsNone(args.input_file)
        self.assertIsNone(args.doc_no)
        self.assertEqual("operator", args.requested_by)

    def test_create_preserves_only_supported_submit_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.xlsx"
            source.write_bytes(b"test")
            paths = app_paths(root)
            args = build_submit_args(
                SubmissionForm(
                    workflow_id="e10.requisition.create",
                    request_no=" REQ-GUI-001 ", input_file=str(source),
                    requested_by="operator", timeout_seconds="900"),
                paths)
        self.assertEqual("REQ-GUI-001", args.request_no)
        self.assertEqual(source, args.input_file)
        self.assertEqual(900, args.timeout_seconds)
        self.assertEqual(paths.artifact_root, args.artifact_root)
        self.assertFalse(hasattr(args, "command"))
        self.assertFalse(hasattr(args, "password"))

    def test_verify_accepts_doc_and_optional_request_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = build_submit_args(
                SubmissionForm(
                    workflow_id="e10.requisition.verify",
                    doc_no="3110-26090001", request_no="REQ-OLD"),
                app_paths(Path(temporary)))
        self.assertEqual("3110-26090001", args.doc_no)
        self.assertEqual("REQ-OLD", args.request_no)

    def test_unknown_workflow_and_bad_timeout_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = app_paths(Path(temporary))
            with self.assertRaisesRegex(ValueError, "不支持工作流"):
                build_submit_args(
                    SubmissionForm(workflow_id="run.anything"), paths)
            with self.assertRaisesRegex(ValueError, "必须是整数"):
                build_submit_args(
                    SubmissionForm(
                        workflow_id="e10.session.login",
                        timeout_seconds="forever"),
                    paths)


if __name__ == "__main__":
    unittest.main()
