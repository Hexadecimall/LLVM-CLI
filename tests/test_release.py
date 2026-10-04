#!/usr/bin/env python3
"""Offline release-asset and POSIX installer regression tests."""

from pathlib import Path
import os
import subprocess
import struct
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tools"))
from prepare_release import split_binary, split_gzip_binary, validate_host_format  # noqa: E402


class ReleaseInstallerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.binary = self.root / "fixture-llvm"
        self.binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --version ]; then echo 'LLVM-CLI 1.7.0'; exit 0; fi\n"
            "exit 1\n",
            encoding="ascii",
        )
        self.binary.chmod(0o755)
        manifest = ["llvm-cli-release-v1"]
        manifest.extend(split_binary("linux-x86_64", self.binary, self.assets, 17))
        (self.assets / "llvm-cli-manifest-v1.txt").write_text(
            "\n".join(manifest) + "\n", encoding="ascii"
        )

        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self._executable(
            "uname",
            "#!/bin/sh\n"
            "case \"$1\" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n",
        )
        self._executable(
            "curl",
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -o) output=$2; shift 2 ;;\n"
            "    https://*) url=$1; shift ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "cp \"$LLVM_CLI_TEST_ASSETS/${url##*/}\" \"$output\"\n",
        )

    def _executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="ascii")
        path.chmod(0o755)

    def _install(self, directory, version="v-test", force=False):
        environment = os.environ.copy()
        environment["PATH"] = str(self.fake_bin) + os.pathsep + environment["PATH"]
        environment["LLVM_CLI_TEST_ASSETS"] = str(self.assets)
        return subprocess.run(
            ["sh", str(PROJECT / "install/install.sh"), "--version", version,
             "--install-dir", str(directory), *(["--force"] if force else [])],
            env=environment, text=True, capture_output=True, timeout=30,
        )

    def test_installs_verified_parts_as_one_executable(self):
        directory = self.root / "installed"
        result = self._install(directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((directory / "llvm").read_bytes(), self.binary.read_bytes())
        self.assertEqual((directory / "LLVM").read_bytes(), self.binary.read_bytes())

    def test_installs_gzip_parts_as_one_executable(self):
        for part in self.assets.glob("*.part*"):
            part.unlink()
        manifest = ["llvm-cli-release-v2"]
        manifest.extend(split_gzip_binary(
            "linux-x86_64", self.binary, self.assets, 17))
        (self.assets / "llvm-cli-manifest-v1.txt").write_text(
            "\n".join(manifest) + "\n", encoding="ascii"
        )
        directory = self.root / "gzip-installed"
        result = self._install(directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((directory / "llvm").read_bytes(), self.binary.read_bytes())

    def test_keeps_newer_local_version_without_downloading_parts(self):
        directory = self.root / "newer"
        directory.mkdir()
        installed = directory / "llvm"
        installed.write_text(
            "#!/bin/sh\necho 'LLVM-CLI 1.8.0'\n", encoding="ascii"
        )
        installed.chmod(0o755)
        for part in self.assets.glob("*.part*"):
            part.unlink()
        result = self._install(directory, version="v1.7.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("newer than release", result.stdout)
        self.assertIn("LLVM-CLI 1.8.0", installed.read_text(encoding="ascii"))

        split_binary("linux-x86_64", self.binary, self.assets, 17)
        forced = self._install(directory, version="v1.7.0", force=True)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(installed.read_bytes(), self.binary.read_bytes())

    def test_rejects_corrupted_part_without_installing(self):
        first_part = self.assets / "llvm-cli-linux-x86_64.part000"
        first_part.write_bytes(b"x" + first_part.read_bytes()[1:])
        directory = self.root / "not-installed"
        result = self._install(directory)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)
        self.assertFalse((directory / "llvm").exists())

    def test_skips_download_when_installed_binary_matches_manifest(self):
        directory = self.root / "installed"
        first = self._install(directory)
        self.assertEqual(first.returncode, 0, first.stderr)
        for part in self.assets.glob("*.part*"):
            part.unlink()
        second = self._install(directory)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already installed", second.stdout)
        self.assertEqual((directory / "llvm").read_bytes(), self.binary.read_bytes())

    def test_packager_rejects_wrong_host_format(self):
        with self.assertRaisesRegex(ValueError, "native darwin-arm64"):
            validate_host_format("darwin-arm64", self.binary)
        elf = self.root / "tiny-elf"
        header = bytearray(64)
        header[:5] = b"\x7fELF\x02"
        struct.pack_into("<H", header, 18, 62)
        elf.write_bytes(header)
        validate_host_format("linux-x86_64", elf)
        with self.assertRaisesRegex(ValueError, "native linux-aarch64"):
            validate_host_format("linux-aarch64", elf)

    def test_does_not_replace_an_unrelated_llvm(self):
        directory = self.root / "occupied"
        directory.mkdir()
        existing = directory / "llvm"
        existing.write_text("#!/bin/sh\necho unrelated\n", encoding="ascii")
        existing.chmod(0o755)
        result = self._install(directory)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not LLVM-CLI", result.stderr)
        self.assertIn("unrelated", existing.read_text(encoding="ascii"))


if __name__ == "__main__":
    unittest.main()
