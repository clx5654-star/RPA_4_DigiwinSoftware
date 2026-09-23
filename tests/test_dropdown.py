import unittest

from rpa_core import (
    DropdownAction,
    DropdownScrollJump,
    DropdownStalled,
    DropdownTargetSkipped,
    decide_dropdown_action,
)


class DropdownDecisionTests(unittest.TestCase):
    def test_three_visible_items_cap_batch_at_two(self):
        decision = decide_dropdown_action(("A", "B", "C"), "Z", 8)
        self.assertEqual(DropdownAction.SCROLL, decision.action)
        self.assertEqual(2, decision.scroll_steps)

    def test_visible_target_is_selected_without_scroll(self):
        decision = decide_dropdown_action(("A", "TARGET", "C"), "TARGET", 8)
        self.assertEqual(DropdownAction.SELECT, decision.action)
        self.assertEqual(0, decision.scroll_steps)

    def test_target_visible_before_batch_then_missing_is_rejected(self):
        with self.assertRaises(DropdownTargetSkipped):
            decide_dropdown_action(
                ("C", "D", "E"), "TARGET", 8,
                previous_visible=("A", "TARGET", "C"),
                previous_scroll_steps=2,
                target_selected=False,
            )

    def test_three_unchanged_observations_are_rejected(self):
        visible = ("A", "B", "C")
        count = 0
        for _ in range(2):
            decision = decide_dropdown_action(
                visible, "Z", 8, previous_visible=visible,
                previous_scroll_steps=2, unchanged_count=count,
            )
            count = decision.unchanged_count
        with self.assertRaises(DropdownStalled):
            decide_dropdown_action(
                visible, "Z", 8, previous_visible=visible,
                previous_scroll_steps=2, unchanged_count=count,
            )

    def test_scroll_jump_larger_than_plan_is_rejected(self):
        with self.assertRaises(DropdownScrollJump):
            decide_dropdown_action(
                ("X", "Y", "Z"), "TARGET", 8,
                previous_visible=("A", "B", "C"),
                previous_scroll_steps=1,
            )


if __name__ == "__main__":
    unittest.main()
