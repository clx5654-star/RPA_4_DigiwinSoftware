import json
import os
import tempfile
import unittest
from pathlib import Path

from e10_credentials import (CredentialStoreError, load_profile,
                             save_profile)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
class CredentialStoreTests(unittest.TestCase):
    def save_or_skip_if_dpapi_profile_is_unavailable(self, *args, **kwargs):
        try:
            return save_profile(*args, **kwargs)
        except CredentialStoreError as exc:
            # Codex/CI may deliberately run under a restricted token without
            # access to the interactive user's DPAPI master key.  The same
            # tests are also run in the real Windows user context.
            if "winerror=2" in str(exc):
                self.skipTest("current process has no Windows DPAPI user profile")
            raise

    def test_password_is_dpapi_encrypted_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "credentials"
            path = self.save_or_skip_if_dpapi_profile_is_unavailable(
                store, profile="test_ft", username="TESTUSER",
                account_set="FT", password="not-a-real-password")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("not-a-real-password", raw)
            payload = json.loads(raw)
            self.assertEqual("WINDOWS_DPAPI_CURRENT_USER",
                             payload["password_protection"])
            loaded = load_profile(store, "test_ft")
            self.assertEqual("TESTUSER", loaded.username)
            self.assertEqual("FT", loaded.account_set)
            self.assertEqual("not-a-real-password", loaded.password)

    def test_existing_profile_is_not_overwritten_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "credentials"
            self.save_or_skip_if_dpapi_profile_is_unavailable(
                store, profile="test_ft", username="A",
                account_set="FT", password="one")
            with self.assertRaises(CredentialStoreError):
                save_profile(store, profile="test_ft", username="B",
                             account_set="FT", password="two")
            self.assertEqual("A", load_profile(store, "test_ft").username)

    def test_profile_name_cannot_escape_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CredentialStoreError):
                save_profile(Path(temporary), profile="../escape",
                             username="A", account_set="FT", password="x")


if __name__ == "__main__":
    unittest.main()
