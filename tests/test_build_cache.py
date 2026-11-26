"""Protect generated inputs across host-build storage switches."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
BINARY = os.environ.get("LLVM_CLI_BINARY")


@unittest.skipUnless(BINARY, "set LLVM_CLI_BINARY to the built host executable")
class BuildCacheTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT / "build-work/cache-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "build"
        self.root.mkdir()
        (self.root / "CMakeCache.txt").write_text("generated fixture\n")
        self.object = self.root / "component.o"
        self.content = b"compiled input and relocation data\n" * 4096
        self.object.write_bytes(self.content)

    def run_cache(self, operation):
        return subprocess.run([BINARY, "python", str(PROJECT / "tools/build_cache.py"),
                               operation, str(self.root)], text=True,
                              capture_output=True, timeout=60)

    def test_round_trip_preserves_inputs_metadata_sources_and_generators(self):
        self.object.chmod(0o640)
        os.utime(self.object, ns=(1700000000000000000, 1700000000123456789))
        before = self.object.stat()
        source = self.root / "source.cpp"
        source.write_bytes(self.content)
        generator = self.root / "generator"
        generator.write_bytes(self.content)
        shared = self.root / "shared.o"
        os.link(source, shared)
        frozen = self.run_cache("freeze")
        self.assertEqual(frozen.returncode, 0, frozen.stderr)
        self.assertFalse(self.object.exists())
        self.assertTrue(source.exists() and generator.exists() and shared.exists())
        restored = self.run_cache("thaw")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.object.read_bytes(), self.content)
        after = self.object.stat()
        self.assertEqual((after.st_mode, after.st_mtime_ns), (before.st_mode, before.st_mtime_ns))
        self.assertFalse(list((self.parent / "build-frozen/files").rglob("*.zst")))

    def test_corruption_never_restores_unverified_bytes(self):
        self.assertEqual(self.run_cache("freeze").returncode, 0)
        frame = self.parent / "build-frozen/files/component.o.zst"
        frame.write_bytes(b"invalid frame")
        restored = self.run_cache("thaw")
        self.assertNotEqual(restored.returncode, 0)
        self.assertFalse(self.object.exists())
        self.assertTrue(frame.exists())

    def test_changed_raw_input_is_never_overwritten(self):
        self.assertEqual(self.run_cache("freeze").returncode, 0)
        self.object.write_bytes(b"a newer generated input")
        restored = self.run_cache("thaw")
        self.assertNotEqual(restored.returncode, 0)
        self.assertEqual(self.object.read_bytes(), b"a newer generated input")
        self.assertTrue((self.parent / "build-frozen/files/component.o.zst").exists())

    def test_partial_journal_append_can_resume_without_losing_inputs(self):
        self.assertEqual(self.run_cache("freeze").returncode, 0)
        journal = self.parent / "build-frozen/journal.jsonl"
        with journal.open("ab") as output:
            output.write(b'{"path":')
        other = self.root / "other.o"
        other.write_bytes(self.content + b"second")
        self.assertEqual(self.run_cache("freeze").returncode, 0)
        restored = self.run_cache("thaw")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.object.read_bytes(), self.content)
        self.assertEqual(other.read_bytes(), self.content + b"second")


if __name__ == "__main__":
    unittest.main()
