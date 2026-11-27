#!/usr/bin/env python3
"""Build the Windows C++ runtime with LLVM-CLI and retained Windows resources."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from macos_build import PROJECT, RELEASE, workspace, build_environment
from prepare_linux_runtime import build_cxx_runtime

def prepare_imports(volume, compiler, zig_lib):
    """Reuse upstream MinGW DLL exports and GUID definitions."""
    sources = zig_lib / "libc/mingw"
    target = PROJECT / "recovered/llvm-all/inmemory/payload/root/targets/x86_64-windows-gnu"
    imports = volume / "runtime-work/windows-gnu/imports"
    imports.mkdir(parents=True, exist_ok=True)
    environment = build_environment(volume)
    cc = [compiler, "cc", "-compile-target", "x86_64-windows-gnu",
          "--sysroot=" + str(target), "-isystem", target / "include/any-windows-any"]

    def run(arguments):
        subprocess.run(list(map(str, arguments)), env=environment, check=True)

    for name in ("version", "psapi", "shell32", "ole32", "advapi32",
                 "ws2_32", "ntdll", "winhttp", "crypt32"):
        library = imports / ("lib" + name + ".a")
        if library.is_file():
            continue
        retained = target / "lib" / (name + ".lib")
        if retained.is_file():
            library.symlink_to(retained)
            continue
        definitions = sources / "lib-common" / (name + ".def")
        if not definitions.is_file():
            definitions = imports / (name + ".def")
            run([*cc, "-E", "-P", "-x", "assembler-with-cpp", "-DDEF_X64",
                 "-I", sources / "def-include", sources / "lib-common" / (name + ".def.in"),
                 "-o", definitions])
        run([compiler, "dlltool", "-m", "i386:x86-64", "-d", definitions, "-l", library])
    library = imports / "libuuid.a"
    if not library.is_file():
        obj = imports / "uuid.obj"
        run([*cc, "-O3", "-c", sources / "libsrc/uuid.c", "-o", obj])
        run([compiler, "ar", "rcs", library, obj])
    return imports


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
    parser.add_argument("--zig-lib", type=Path)
    parser.add_argument("--imports-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    volume = workspace()
    if args.imports_only and not args.zig_lib:
        parser.error("--imports-only requires --zig-lib")
    if args.zig_lib:
        print(prepare_imports(volume, args.compiler.absolute(), args.zig_lib.absolute()))
    if not args.imports_only:
        print(prepare(volume, args.compiler.absolute(), args.jobs))
