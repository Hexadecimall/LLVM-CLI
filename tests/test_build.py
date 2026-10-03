#!/usr/bin/env python3
"""End-to-end tests for target aliases and the embedded incremental builder."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
BINARY = os.environ.get("LLVM_CLI_BINARY")


@unittest.skipUnless(BINARY, "set LLVM_CLI_BINARY to the built host executable")
class IncrementalBuildTests(unittest.TestCase):
    def test_only_changed_header_dependents_recompile(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            main = root / "main.cpp"
            helper = root / "helper.cpp"
            header = root / "message.hpp"
            main.write_text("int answer();\nint main() { return answer() - 42; }\n")
            helper.write_text('#include "message.hpp"\nint answer() { return value; }\n')
            header.write_text("constexpr int value = 42;\n")
            command = [BINARY, "build", "--build-dir", str(root / "build"),
                       "--output", str(root / "app"), str(main), str(helper)]

            first = subprocess.run(command, text=True, capture_output=True,
                                   timeout=180)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.count("build: compile"), 2)
            self.assertEqual(subprocess.run([str(root / "app")], timeout=5).returncode, 0)

            second = subprocess.run(command, text=True, capture_output=True,
                                    timeout=180)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("build: compile", second.stdout)
            self.assertIn("build: up to date", second.stdout)

            header.write_text("constexpr int value = 43;\n")
            third = subprocess.run(command, text=True, capture_output=True,
                                   timeout=180)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertIn(f"build: compile {helper}", third.stdout)
            self.assertNotIn(f"build: compile {main}", third.stdout)
            self.assertIn("build: link", third.stdout)
            self.assertEqual(subprocess.run([str(root / "app")], timeout=5).returncode, 1)

    def test_common_triple_uses_embedded_musl_runtime(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            output = Path(scratch) / "linux-app"
            result = subprocess.run(
                [BINARY, "cc", "-compile-target", "x86_64-unknown-linux-musl",
                 str(PROJECT / "tests/smoke.c"), "-o", str(output)],
                text=True, capture_output=True, timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes()[:4], b"\x7fELF")


if __name__ == "__main__":
    unittest.main()
