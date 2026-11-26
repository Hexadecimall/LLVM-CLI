#!/usr/bin/env python3
"""Build the Windows C++ runtime with LLVM-CLI and retained Windows resources."""

import argparse
import json
from pathlib import Path
import shutil

from macos_build import PROJECT, RELEASE, workspace
from prepare_linux_runtime import build_cxx_runtime


def prepare(volume, compiler, jobs):
    target = PROJECT / "recovered/llvm-all/inmemory/payload/root/targets/x86_64-windows-gnu"
    if not (target / "lib/zigc.lib").is_file():
        raise RuntimeError("retained Windows target resources are missing")
    root = volume / "runtime-work/windows-gnu"
    root.mkdir(parents=True, exist_ok=True)
    flags = ["-compile-target", "x86_64-windows-gnu", "--sysroot=" + str(target),
             "-isystem", str(target / "include/any-windows-any"),
             "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"]
    runtime = build_cxx_runtime(volume, root, compiler, flags, jobs,
                                system="Windows", triple="x86_64-windows-gnu")
    (root / "resources.json").write_text(json.dumps({
        "llvm_version": RELEASE["version"], "target": "x86_64-windows-gnu",
        "project_compiler": "LLVM-CLI", "system_runtime": "Windows system DLLs",
        "headers_and_C_startup": "retained embedded target resources",
    }, indent=2) + "\n")
    return runtime


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    print(prepare(workspace(), args.compiler.absolute(), args.jobs))
