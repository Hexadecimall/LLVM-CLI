#!/usr/bin/env python3
"""Check embedded clangd's standard and local header resolution."""

from pathlib import Path
import os
import subprocess
import unittest


PROJECT = Path(__file__).resolve().parent.parent
BINARY = os.environ.get("LLVM_CLI_BINARY")


@unittest.skipUnless(BINARY, "set LLVM_CLI_BINARY to the built host executable")
class EmbeddedClangdTests(unittest.TestCase):
    def test_clangd_resolves_standard_and_project_headers(self):
        source = PROJECT / "tests/editor-fixture/main.cpp"
        environment = os.environ.copy()
        environment.pop("SDKROOT", None)
        environment.update(PATH="/nonexistent", DEVELOPER_DIR="/nonexistent")
        result = subprocess.run(
            [BINARY, "clangd", f"--check={source}", "--log=error"],
            cwd=PROJECT, env=environment, text=True, capture_output=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Failed to auto-detect resource directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
