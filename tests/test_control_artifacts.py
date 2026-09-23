import json
import tempfile
import unittest
from pathlib import Path

from rpa_control.artifacts import (ArtifactError, import_artifact,
                                   load_and_verify_artifact)


class ControlArtifactTests(unittest.TestCase):
    def test_file_is_copied_hashed_and_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.xlsx"
            source.write_bytes(b"offline-xlsx-placeholder")
            manifest = import_artifact(
                source, root / "controlled", frozenset({".xlsx"}),
                artifact_id="FILE-ABC123")
            loaded, controlled = load_and_verify_artifact(
                root / "controlled", manifest.artifact_id,
                manifest.sha256, frozenset({".xlsx"}))
            self.assertEqual(manifest.sha256, loaded.sha256)
            self.assertNotEqual(source.resolve(), controlled.resolve())
            self.assertEqual(source.read_bytes(), controlled.read_bytes())

    def test_tampered_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.xlsx"
            source.write_bytes(b"before")
            manifest = import_artifact(
                source, root / "controlled", frozenset({".xlsx"}),
                artifact_id="FILE-TAMPER")
            Path(manifest.controlled_path).write_bytes(b"after")
            with self.assertRaises(ArtifactError):
                load_and_verify_artifact(
                    root / "controlled", manifest.artifact_id,
                    manifest.sha256, frozenset({".xlsx"}))

    def test_traversal_unc_and_illegal_extension_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = root / "input.txt"
            text.write_text("x", encoding="utf-8")
            with self.assertRaises(ArtifactError):
                import_artifact(text, root / "controlled",
                                frozenset({".xlsx"}))
            with self.assertRaises(ArtifactError):
                import_artifact(Path("..") / "escape.xlsx",
                                root / "controlled", frozenset({".xlsx"}))
            with self.assertRaises(ArtifactError):
                import_artifact(Path(r"\\server\share\x.xlsx"),
                                root / "controlled", frozenset({".xlsx"}))

    def test_manifest_cannot_redirect_outside_artifact_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.xlsx"
            source.write_bytes(b"x")
            manifest = import_artifact(
                source, root / "controlled", frozenset({".xlsx"}),
                artifact_id="FILE-REDIRECT")
            path = root / "controlled" / manifest.artifact_id / "artifact_manifest.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["controlled_path"] = str(source)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ArtifactError):
                load_and_verify_artifact(
                    root / "controlled", manifest.artifact_id,
                    manifest.sha256, frozenset({".xlsx"}))


if __name__ == "__main__":
    unittest.main()
