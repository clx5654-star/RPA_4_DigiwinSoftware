import unittest
from decimal import Decimal
from pathlib import Path

import e10_purchase_requisition
import e10_requisition
from rpa_core import GridSnapshot, RequisitionRecord, correlation_value
from e10_purchase_requisition import (ITEM_LOOKUP_HOTKEY,
                                      _classify_discardable_draft_row,
                                      _row_index_from_cell_name)


class RequisitionInputTests(unittest.TestCase):
    def test_grid_snapshot_is_available_to_live_reconcile_paths(self):
        self.assertIs(GridSnapshot, e10_purchase_requisition.GridSnapshot)

    def test_legacy_entrypoint_delegates_to_purchase_requisition(self):
        self.assertIs(e10_requisition.main, e10_purchase_requisition.main)

    def test_normalizes_recorded_xlsx_values(self):
        record = RequisitionRecord.from_mapping({
            "单据类型": 3110,
            "单据名称": "请购单",
            "申请人": "王中前",
            "需求日期": 20261017,
            "品号": "21302050002R",
            "请购数量": 5000,
        })
        self.assertEqual("3110", record.document_type)
        self.assertEqual("2026-10-17", record.required_date)
        self.assertEqual(Decimal("5000"), record.quantity)
        self.assertEqual("5000", record.quantity_text)

    def test_business_request_number_is_optional_when_cli_will_supply_it(self):
        record = RequisitionRecord.from_mapping({
            "单据类型": 3110, "单据名称": "请购单", "申请人": "测试人",
            "需求日期": 20261017, "品号": "X", "请购数量": 1,
            "业务请求号": "REQ-001",
        })
        self.assertEqual("REQ-001", record.request_no)
        self.assertTrue(record.payload_fingerprint.startswith(
            "e10-requisition-payload:"))

    def test_correlation_injection_is_excluded_from_payload_fingerprint(self):
        common = {
            "单据类型": 3110, "单据名称": "请购单", "申请人": "测试人",
            "需求日期": 20261017, "品号": "X", "请购数量": 1,
        }
        first = RequisitionRecord.from_mapping({
            **common, "业务请求号": "REQ-A"})
        second = RequisitionRecord.from_mapping({
            **common, "业务请求号": "REQ-B"})
        self.assertEqual(first.payload_fingerprint, second.payload_fingerprint)
        self.assertNotEqual(correlation_value(first.request_no),
                            correlation_value(second.request_no))

    def test_missing_required_column_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少列"):
            RequisitionRecord.from_mapping({"单据类型": 3110})

    def test_non_positive_quantity_is_rejected(self):
        row = {
            "单据类型": 3110, "单据名称": "请购单", "申请人": "测试人",
            "需求日期": 20261017, "品号": "X", "请购数量": 0,
        }
        with self.assertRaisesRegex(ValueError, "必须大于 0"):
            RequisitionRecord.from_mapping(row)

    def test_probe_uses_bounded_tree_walk_not_unbounded_descendants(self):
        source = (Path(__file__).parents[1]
                  / "e10_purchase_requisition.py").read_text(encoding="utf-8")
        self.assertNotIn("window.descendants()", source)
        self.assertIn("PROBE_MAX_DEPTH", source)
        self.assertIn("PROBE_NODE_LIMIT", source)
        self.assertIn("maxDepth=PROBE_MAX_DEPTH", source)

    def test_lookup_result_row_index_is_parsed(self):
        self.assertEqual(0, _row_index_from_cell_name("品号 row 0"))
        self.assertEqual(12, _row_index_from_cell_name("品号 row 12"))

    def test_lookup_result_without_row_index_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "缺少稳定行号"):
            _row_index_from_cell_name("品号")

    def test_normal_item_lookup_uses_f2(self):
        self.assertEqual("{F2}", ITEM_LOOKUP_HOTKEY)

    def test_blank_placeholder_row_is_discardable_header_only_stage(self):
        record = RequisitionRecord.from_mapping({
            "单据类型": 3110, "单据名称": "请购单", "申请人": "测试人",
            "需求日期": 20261017, "品号": "ITEM-1", "请购数量": 5,
        })
        self.assertEqual(
            "header_only_with_blank_row",
            _classify_discardable_draft_row(record, "", "0.00"))

    def test_nonempty_mismatched_row_is_never_discarded(self):
        record = RequisitionRecord.from_mapping({
            "单据类型": 3110, "单据名称": "请购单", "申请人": "测试人",
            "需求日期": 20261017, "品号": "ITEM-1", "请购数量": 5,
        })
        with self.assertRaisesRegex(RuntimeError, "拒绝代替用户丢弃"):
            _classify_discardable_draft_row(record, "OTHER", "5")


if __name__ == "__main__":
    unittest.main()
