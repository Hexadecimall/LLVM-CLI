#!/usr/bin/env python3
"""End-to-end tests for target aliases and the embedded incremental builder."""

from pathlib import Path
import os
import subprocess
import struct
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
BINARY = os.environ.get("LLVM_CLI_BINARY")


@unittest.skipUnless(BINARY, "set LLVM_CLI_BINARY to the built host executable")
class IncrementalBuildTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "embedded Darwin resources")
    def test_darwin_c_headers_and_linker_without_xcode_discovery(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            source = root / "main.c"
            source.write_text('#include <stdio.h>\nint main(void) { return puts("embedded") < 0; }\n')
            environment = os.environ.copy()
            environment.pop("SDKROOT", None)
            environment.update(PATH="/nonexistent", DEVELOPER_DIR="/nonexistent")
            configured = subprocess.run([BINARY, "env"], env=environment, text=True,
                                        capture_output=True, timeout=180)
            self.assertIn("SDKROOT=/__llvm/targets/macos", configured.stdout)
            built = subprocess.run([BINARY, "cc", "-O3", "-Werror", "-Wall", "-Wextra",
                                    str(source), "-o", str(root / "app")], env=environment,
                                   text=True, capture_output=True, timeout=180)
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(subprocess.check_output([str(root / "app")], text=True), "embedded\n")

    def test_compiler_end_of_options(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            source = root / "main.c"
            source.write_text("#include <stddef.h>\nint main(void) { return 0; }\n")
            output = root / "app"
            result = subprocess.run([BINARY, "clang", "-o", str(output), "--", str(source)],
                                    text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(subprocess.run([str(output)], timeout=10).returncode, 0)

    def test_renamed_binary_remains_a_dispatcher(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            renamed = Path(scratch) / "custom-compiler-suite"
            renamed.symlink_to(Path(BINARY).resolve())
            result = subprocess.run([str(renamed), "--version"], text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LLVM-CLI", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "Mach-O static linking limitation")
    def test_static_request_never_silently_links_system_runtime(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            source = Path(scratch) / "main.c"
            source.write_text("int main(void) { return 0; }\n")
            output = Path(scratch) / "app"
            result = subprocess.run([BINARY, "clang", "-static", str(source), "-o", str(output)],
                                    text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to produce a dynamically linked executable", result.stderr)
            self.assertFalse(output.exists())

    def test_windows_static_request_never_silently_imports_runtime(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            source = Path(scratch) / "main.c"
            source.write_text("int main(void) { return 0; }\n")
            output = Path(scratch) / "app.exe"
            result = subprocess.run(
                [BINARY, "clang", "-compile-target", "x86_64-windows-gnu", "-static",
                 str(source), "-o", str(output)],
                text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("import-free Windows executable", result.stderr)
            self.assertFalse(output.exists())

    @unittest.skipUnless(sys.platform == "darwin", "macOS static C++ runtime")
    def test_native_static_cpp_filesystem(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            source = root / "filesystem.cpp"
            source.write_text(
                '#include <filesystem>\n#include <iostream>\n'
                '#if __cplusplus != 201703L\n#error expected C++17 default\n#endif\n'
                'int main() { std::cout << std::filesystem::current_path().string(); }\n')
            output = root / "filesystem"
            built = subprocess.run([BINARY, "clang++", str(source), "-o", str(output)],
                                   text=True, capture_output=True, timeout=180)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(output)], cwd=root, text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout).resolve(), root.resolve())
            dependencies = subprocess.check_output(["otool", "-L", str(output)], text=True)
            self.assertNotIn("libc++", dependencies)
            self.assertNotIn("libunwind", dependencies)

    @unittest.skipUnless(sys.platform == "darwin", "macOS cross C++ runtime")
    def test_x86_macos_cpp_uses_embedded_runtime(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            source = root / "main.cpp"
            source.write_text('#include <iostream>\nint main() { std::cout << "cross"; }\n')
            output = root / "app"
            environment = os.environ.copy()
            environment.pop("SDKROOT", None)
            environment.update(PATH="/nonexistent", DEVELOPER_DIR="/nonexistent")
            built = subprocess.run([BINARY, "c++", "-compile-target", "x86_64-macos",
                                    "-Werror", str(source), "-o", str(output)],
                                   env=environment, text=True, capture_output=True, timeout=180)
            self.assertEqual(built.returncode, 0, built.stderr)
            with output.open("rb") as image:
                self.assertEqual(struct.unpack("<II", image.read(8)), (0xFEEDFACF, 0x01000007))
            dependencies = subprocess.check_output(["otool", "-L", str(output)], text=True)
            self.assertNotIn("libc++", dependencies)
            self.assertIn("/usr/lib/libSystem.B.dylib", dependencies)

    def test_direct_clang_incremental_reuses_unchanged_objects(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            header = root / "answer.h"
            source = root / "main.c"
            header.write_text("#define ANSWER 42\n")
            source.write_text('#include "answer.h"\nint main(void) { return ANSWER - 42; }\n')
            command = [BINARY, "clang", "-incremental", "-build-dir",
                       str(root / "objects"), "-I", str(root), str(source),
                       "-O2", "-o", str(root / "app")]
            first = subprocess.run(command, text=True, capture_output=True,
                                   timeout=180)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("build: compile", first.stdout)
            self.assertEqual(subprocess.run([str(root / "app")], timeout=5).returncode, 0)

            second = subprocess.run(command, text=True, capture_output=True,
                                    timeout=180)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("build: compile", second.stdout)
            self.assertIn("build: up to date", second.stdout)

            header.write_text("#define ANSWER 43\n")
            third = subprocess.run(command, text=True, capture_output=True,
                                   timeout=180)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertIn("build: compile", third.stdout)
            self.assertEqual(subprocess.run([str(root / "app")], timeout=5).returncode, 1)

    def test_direct_clang_incremental_object(self):
        validation = PROJECT / "validation"
        validation.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=validation) as scratch:
            root = Path(scratch)
            source = root / "main.c"
            source.write_text("int answer(void) { return 42; }\n")
            command = [BINARY, "clang", "-incremental", "-build-dir",
                       str(root / "objects"), "-c", str(source), "-o",
                       str(root / "main.o")]
            first = subprocess.run(command, text=True, capture_output=True,
                                   timeout=180)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((root / "main.o").is_file())
            second = subprocess.run(command, text=True, capture_output=True,
                                    timeout=180)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("build: unchanged", second.stdout)

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
