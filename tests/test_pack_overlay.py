#!/usr/bin/env python3
"""Verify sparse payload updates reuse the baseline without changing it."""

import ctypes
import ctypes.util
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


PACKER = os.environ.get("LLVM_PACK_BINARY")
ZSTD_LIBRARY = os.environ.get("ZSTD_LIBRARY") or ctypes.util.find_library("zstd")


def unpack_files(path):
    data = path.read_bytes()
    magic, version, count, _ = struct.unpack_from("<8sIIQ", data)
    if magic != b"LLVMPK02" or version != 2:
        raise AssertionError("invalid pack header")
    zstd = ctypes.CDLL(ZSTD_LIBRARY)
    zstd.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_void_p, ctypes.c_size_t]
    zstd.ZSTD_decompress.restype = ctypes.c_size_t
    offset = 24
    files = {}
    for _ in range(count):
        length, kind, _, _, size, stored, position = struct.unpack_from(
            "<HBBIQQQ", data, offset)
        offset += 32
        name = data[offset:offset + length].decode()
        offset += length
        if kind != 2:
            continue
        output = ctypes.create_string_buffer(size)
        result = zstd.ZSTD_decompress(output, size,
                                      data[position:position + stored], stored)
        if result != size:
            raise AssertionError("pack decompression failed")
        files[name] = output.raw
    return files


@unittest.skipUnless(PACKER and ZSTD_LIBRARY,
                     "set LLVM_PACK_BINARY and provide a discoverable ZSTD_LIBRARY")
class PackOverlayTests(unittest.TestCase):
    def test_overrides_and_exclusions_preserve_baseline(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            base = root / "base"
            overlay = root / "overlay"
            base.mkdir()
            overlay.mkdir()
            (base / "replace").write_text("old")
            (base / "keep").write_text("retained")
            (base / "retired").write_text("retired")
            (overlay / "replace").write_text("new")
            (overlay / "add").write_text("added")
            (overlay / ".pack-exclude").write_text("retired\n")
            output = root / "output.pack"
            subprocess.run([PACKER, str(base), str(output), str(overlay)],
                           check=True, capture_output=True)
            self.assertEqual(unpack_files(output),
                             {"replace": b"new", "keep": b"retained", "add": b"added"})
            self.assertEqual((base / "replace").read_text(), "old")
            self.assertTrue((base / "retired").is_file())

    def test_original_packer_interface_still_works(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            base = root / "base"
            base.mkdir()
            (base / "file").write_text("unchanged")
            output = root / "output.pack"
            subprocess.run([PACKER, str(base), str(output)],
                           check=True, capture_output=True)
            self.assertEqual(unpack_files(output), {"file": b"unchanged"})

    def test_precompressed_module_replaces_baseline_without_recompression(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            base = root / "base"
            overlay = root / "overlay"
            (base / "bundles").mkdir(parents=True)
            (overlay / "bundles").mkdir(parents=True)
            (base / "bundles/tool.bundle").write_bytes(b"old module")
            module = root / "module"
            module.write_bytes(b"new module" * 1000)
            compressed = overlay / "bundles/tool.bundle.zst"
            subprocess.run([PACKER, "--compress", str(module), str(compressed)],
                           check=True, capture_output=True)
            output = root / "output.pack"
            subprocess.run([PACKER, str(base), str(output), str(overlay)],
                           check=True, capture_output=True)
            self.assertEqual(unpack_files(output),
                             {"bundles/tool.bundle": module.read_bytes()})
            self.assertIn(compressed.read_bytes(), output.read_bytes())


if __name__ == "__main__":
    unittest.main()
