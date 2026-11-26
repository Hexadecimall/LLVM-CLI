#!/usr/bin/env python3
"""Ensure missing recovered inputs cannot start a build or erase its manifest."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_all_tool_bundles as bundles  # noqa: E402
import build_final as final  # noqa: E402
import build_native_bundles as native  # noqa: E402
from prepare_native_payload import static_recipes  # noqa: E402


class BuildInputTests(unittest.TestCase):
    def test_reconfigured_tree_ignores_obsolete_link_recipes(self):
        with tempfile.TemporaryDirectory() as scratch:
            build = Path(scratch)
            commands = {
                "current-tool": "clang++ -o bin/current-tool main.o",
                "obsolete-tool": "clang++ -o bin/obsolete-tool main.o",
                "current-api": "ar qc lib/libcurrent.a api.o",
                "obsolete-api": "ar qc lib/libobsolete.a api.o",
            }
            for name, command in commands.items():
                directory = build / "CMakeFiles" / (name + ".dir")
                directory.mkdir(parents=True)
                (directory / "link.txt").write_text(command)
            (build / "CMakeFiles/TargetDirectories.txt").write_text("".join(
                str(build / "CMakeFiles" / (name + ".dir")) + "\n"
                for name in ("current-tool", "current-api")))
            self.assertEqual(set(native.recipes(build)), {"current-tool"})
            self.assertEqual(set(static_recipes(build)), {"libcurrent.a"})

    def test_missing_inputs_stop_before_payload_mutation(self):
        for arguments in (["build_final.py"], ["build_final.py", "--check"]):
            with self.subTest(arguments=arguments), \
                 patch.object(sys, "argv", arguments), \
                 patch.object(final, "missing_build_inputs", return_value=[final.CORE_LINK]), \
                 patch.object(final, "run") as run, \
                 patch.object(final, "scrub_payload_paths") as scrub, \
                 redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(final.main(), 1)
                run.assert_not_called()
                scrub.assert_not_called()
                self.assertIn("No build was started", errors.getvalue())

    def test_check_mode_does_not_build(self):
        with patch.object(sys, "argv", ["build_final.py", "--check"]), \
             patch.object(final, "missing_build_inputs", return_value=[]), \
             patch.object(final, "run") as run:
            self.assertEqual(final.main(), 0)
            run.assert_not_called()

    def test_empty_bundle_input_preserves_manifest(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            build = root / "build"
            output = root / "image/bundles"
            output.parent.mkdir()
            manifest = output.parent / "build-manifest.json"
            manifest.write_text('{"bundles": ["preserved"]}')
            for present in (False, True):
                if present:
                    build.mkdir()
                with self.subTest(present=present), \
                     patch.object(bundles, "BUILD", build), \
                     patch.object(bundles, "OUTPUT", output), \
                     redirect_stderr(io.StringIO()):
                    self.assertEqual(bundles.main(), 1)
                    self.assertFalse(output.exists())
                    self.assertEqual(manifest.read_text(), '{"bundles": ["preserved"]}')


if __name__ == "__main__":
    unittest.main()
