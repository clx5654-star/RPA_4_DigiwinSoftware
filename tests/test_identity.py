import unittest

from rpa_core import (IdentityStrength, correlation_value, identity_key,
                      registration_key, require_dataset_epoch)


class IdentityTests(unittest.TestCase):
    def test_epoch_and_request_form_stable_identity(self):
        self.assertEqual("reset-20260918::REQ-7",
                         identity_key("reset-20260918", "REQ-7"))
        self.assertEqual(
            "reset-20260918::REQ-7::run-a",
            registration_key({
                "run_id": "run-a", "dataset_epoch": "reset-20260918",
                "request_no": "REQ-7"}))

    def test_identity_inputs_cannot_be_blank(self):
        with self.assertRaises(ValueError):
            require_dataset_epoch("")
        with self.assertRaises(ValueError):
            correlation_value("  ")

    def test_identity_strength_vocabulary_is_closed(self):
        self.assertEqual(
            {"REQUEST_FIELD_MATCH", "CONTENT_MATCH", "DOC_NO_ONLY"},
            {value.value for value in IdentityStrength})


if __name__ == "__main__":
    unittest.main()
