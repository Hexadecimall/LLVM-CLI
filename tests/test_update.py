"""Exercise release corruption and bounded, atomic update staging offline."""

import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "llvm_update", Path(__file__).resolve().parent.parent / "install/update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class UpdateTests(unittest.TestCase):
    def fixture(self, compressed=False):
        image = b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", 62) + bytes(2048)
        payload = gzip.compress(image) if compressed else image
        parts = [payload[i:i + 31] for i in range(0, len(payload), 31)]
        version = 2 if compressed else 1
        rows = [f"llvm-cli-release-v{version}",
                f"binary linux-x86_64 {len(image)} {hashlib.sha256(image).hexdigest()} {len(parts)}" +
                (" gzip" if compressed else "")]
        rows += [f"part linux-x86_64 {i} {len(p)} {hashlib.sha256(p).hexdigest()}"
                 for i, p in enumerate(parts)]
        return image, parts, "\n".join(rows)

    def test_streams_raw_and_gzip_without_part_files(self):
        for compressed in (False, True):
            image, parts, text = self.fixture(compressed)
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as scratch:
                destination = Path(scratch) / "llvm"
                def fetch(url):
                    return io.BytesIO(parts[int(url.rsplit("part", 1)[1])])
                with patch("builtins.print"):
                    update.download_image(fetch, "https://example.test", "linux-x86_64",
                                          update.manifest_image(text, "linux-x86_64"), destination)
                self.assertEqual(destination.read_bytes(), image)
                self.assertEqual(list(Path(scratch).iterdir()), [destination])

    def test_rejects_duplicate_missing_and_wrong_platform_parts(self):
        _, _, text = self.fixture()
        for malformed in (text + "\n" + text.splitlines()[-1],
                          "\n".join(text.splitlines()[:-1]),
                          text.replace("linux-x86_64", "windows-x86_64")):
            with self.assertRaises(ValueError):
                update.manifest_image(malformed, "linux-x86_64")

    def test_failed_update_keeps_existing_executable(self):
        image, parts, text = self.fixture()
        parts[-1] = b"x" * len(parts[-1])
        with tempfile.TemporaryDirectory() as scratch:
            installed = Path(scratch) / "llvm"
            installed.write_bytes(image)
            def fetch(url):
                if url.endswith(".txt"):
                    return io.BytesIO(text.encode())
                return io.BytesIO(parts[int(url.rsplit("part", 1)[1])])
            with patch.object(update, "release_opener", return_value=fetch), \
                 patch.object(update, "host_platform", return_value="linux-x86_64"), \
                 patch("builtins.print"), self.assertRaisesRegex(ValueError, "SHA-256"):
                update.main(["--executable", str(installed), "--current-version", "1.7.0",
                             "--version", "v1.8.0"])
            self.assertEqual(installed.read_bytes(), image)
            self.assertEqual(list(Path(scratch).iterdir()), [installed])

    def test_rejects_expansion_beyond_declared_size(self):
        _, parts, text = self.fixture(True)
        text = text.replace(" 2068 ", " 32 ")
        with tempfile.TemporaryDirectory() as scratch, patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "declared size"):
                update.download_image(lambda url: io.BytesIO(parts[int(url.rsplit("part", 1)[1])]),
                    "https://example.test", "linux-x86_64",
                    update.manifest_image(text, "linux-x86_64"), Path(scratch) / "llvm")

    def test_rejects_wrong_cpu_even_with_valid_hash(self):
        with tempfile.TemporaryDirectory() as scratch:
            image = Path(scratch) / "llvm"
            image.write_bytes(self.fixture()[0])
            with self.assertRaisesRegex(ValueError, "native host format"):
                update.validate_image(image, "linux-aarch64")


if __name__ == "__main__":
    unittest.main()
