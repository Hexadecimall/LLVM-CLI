"""Verify Make remains incremental with headers stored inside the compiler."""

from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from remap_embedded_dependencies import remap


class DependencyRemapTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("make"), "Make is required")
    def test_noop_build_and_compiler_replacement_invalidation(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            compiler = root / "tool chain/compiler#1"
            compiler.parent.mkdir()
            compiler.touch()
            (root / "source.c").touch()
            recipe = "from pathlib import Path; Path('output').touch(); " \
                     "p=Path('runs'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
            (root / "compiler_depend.make").write_text(
                "output: source.c /__llvm/include/first.h /__llvm/include/second.h\n"
                "\t@" + shlex.quote(sys.executable) + " -c " + shlex.quote(recipe) + "\n"
                "/__llvm/include/first.h:\n/__llvm/include/second.h:\n")
            self.assertEqual(remap(root, compiler), 1)
            self.assertEqual(remap(root, compiler), 0)
            def build():
                result = subprocess.run(["make", "-f", "compiler_depend.make", "output"],
                                        cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            build()
            build()
            self.assertEqual((root / "runs").read_text(), "x")
            time.sleep(1.05)
            compiler.touch()
            build()
            self.assertEqual((root / "runs").read_text(), "xx")
            build()
            self.assertEqual((root / "runs").read_text(), "xx")

    def test_cmake_internal_paths_keep_spaces_literal(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            compiler = root / "tool chain/compiler"
            record = root / "compiler_depend.internal"
            record.write_text("target.o\n /__llvm/lib/clang/23/include/stddef.h\n")
            unrelated = root / "source.cpp"
            unrelated.write_text('const char *p = "/__llvm/data";\n')
            self.assertEqual(remap(root, compiler), 1)
            self.assertIn(str(compiler), record.read_text())
            self.assertIn("/__llvm/data", unrelated.read_text())


if __name__ == "__main__":
    unittest.main()
