#!/usr/bin/env python3
"""Check source-upgrade patch preservation without compiling or downloading."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from prepare_llvm_source import digest, local_changes, relative_name  # noqa: E402


class SourceUpgradeTests(unittest.TestCase):
    def test_preserves_local_embedding_patch_without_editing_baseline(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "baseline"
            source.mkdir()
            original = b"int embedded = 0;\n"
            changed = b"int embedded = 1;\n"
            (source / "driver.cpp").write_bytes(changed)
            (source / "unchanged.cpp").write_bytes(original)
            archive = root / "baseline.tar.xz"
            with tarfile.open(archive, "w:xz") as tar:
                for name in ("driver.cpp", "unchanged.cpp"):
                    member = tarfile.TarInfo("llvm-project-23.1.1.src/" + name)
                    member.size = len(original)
                    tar.addfile(member, io.BytesIO(original))
            with redirect_stdout(io.StringIO()):
                patch = local_changes(archive, source)
            self.assertIn("--- a/driver.cpp", patch)
            self.assertIn("+int embedded = 1;", patch)
            self.assertNotIn("unchanged.cpp", patch)
            self.assertEqual((source / "driver.cpp").read_bytes(), changed)

    def test_checks_source_hash(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "empty"
            path.touch()
            self.assertEqual(digest(path),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_rejects_archive_traversal(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            relative_name(tarfile.TarInfo("llvm-project-23.1.6.src/../../outside"))


if __name__ == "__main__":
    unittest.main()
