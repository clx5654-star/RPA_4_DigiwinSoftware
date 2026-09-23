import unittest
from pathlib import Path

import e10_recorder as recorder


class FakeControl:
    def __init__(
        self,
        control_type,
        name="",
        automation_id="",
        class_name="",
        children=None,
    ):
        self.ControlTypeName = control_type
        self.Name = name
        self.AutomationId = automation_id
        self.ClassName = class_name
        self._children = list(children or [])
        self._parent = None
        for child in self._children:
            child._parent = self

    def GetChildren(self):
        return self._children

    def GetParentControl(self):
        return self._parent


class RecorderPolicyTests(unittest.TestCase):
    def test_default_cli_disables_expensive_uia_probes(self):
        args = recorder.build_parser().parse_args([])
        self.assertFalse(args.probe_selectors)
        self.assertFalse(args.deep_control_probe)

    def test_production_recorder_does_not_install_global_input_hooks(self):
        source = Path(recorder.__file__).read_text(encoding="utf-8")
        self.assertNotIn("SetWindowsHookExW", source)
        self.assertNotIn("CallNextHookEx", source)

    def test_numeric_automation_id_never_becomes_selector(self):
        snapshot = {
            "control_type": "EditControl",
            "name": "数据编辑器",
            "automation_id": "724772",
            "class_name": "WindowsForms10.EDIT",
            "ancestors": [{"control_type": "CustomControl", "name": "条件行"}],
        }
        candidates = recorder.build_selector_candidates(snapshot)
        self.assertTrue(candidates)
        self.assertFalse(
            any("automation_id" in candidate["target"] for candidate in candidates)
        )
        self.assertEqual("session_dynamic", recorder.automation_id_stability("724772"))

    def test_semantic_automation_id_is_only_a_candidate(self):
        snapshot = {
            "control_type": "ButtonControl",
            "name": "查找(Q)",
            "automation_id": "btnQuery",
            "class_name": "Button",
            "ancestors": [],
        }
        candidates = recorder.build_selector_candidates(snapshot)
        semantic = next(c for c in candidates if c["strategy"] == "semantic_automation_id")
        self.assertEqual("candidate", semantic["stability"])
        self.assertEqual("btnQuery", semantic["target"]["automation_id"])

    def test_unique_probe_requires_complete_scan(self):
        target = FakeControl("ButtonControl", "查找(Q)")
        other = FakeControl("TextControl", "说明")
        scope = FakeControl("WindowControl", "高级查询", children=[target, other])
        candidate = {
            "strategy": "type_name",
            "target": {"control_type": "ButtonControl", "name": "查找(Q)"},
            "ancestor": None,
            "stability": "candidate",
        }
        complete = recorder.probe_selector_candidates(scope, [candidate], 10)
        self.assertEqual(
            "UNIQUE_IN_CURRENT_SCOPE", complete["candidates"][0]["verdict"]
        )
        truncated = recorder.probe_selector_candidates(scope, [candidate], 2)
        self.assertEqual(
            "UNVERIFIED_SCAN_TRUNCATED", truncated["candidates"][0]["verdict"]
        )

    def test_multiple_matches_are_ambiguous(self):
        scope = FakeControl(
            "WindowControl",
            "高级查询",
            children=[
                FakeControl("EditControl", "数据编辑器"),
                FakeControl("EditControl", "数据编辑器"),
            ],
        )
        candidate = {
            "strategy": "type_name",
            "target": {"control_type": "EditControl", "name": "数据编辑器"},
            "ancestor": None,
            "stability": "candidate",
        }
        result = recorder.probe_selector_candidates(scope, [candidate], 10)
        self.assertEqual("AMBIGUOUS", result["candidates"][0]["verdict"])

    def test_sensitive_value_has_no_plaintext_or_hash(self):
        protected = recorder.protected_value(
            "Secret-123", sensitive=True, redact_all=False
        )
        self.assertEqual("***", protected["display"])
        self.assertNotIn("sha256", protected)
        self.assertNotIn("Secret", repr(protected))

    def test_mouse_drag_is_distinguished_from_click(self):
        self.assertEqual(
            "CLICK", recorder.classify_mouse_action("left", (10, 10), (12, 12), 0.1)
        )
        self.assertEqual(
            "DRAG", recorder.classify_mouse_action("left", (10, 10), (50, 10), 0.5)
        )


if __name__ == "__main__":
    unittest.main()
