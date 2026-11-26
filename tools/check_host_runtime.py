#!/usr/bin/env python3
"""Link hosted C++ probes with the prepared target archives and inspect dependencies.

Cross-host execution is intentionally a separate check on the destination OS.
"""

import argparse
from pathlib import Path
import re
import shutil
import subprocess

from macos_build import PROJECT, workspace, build_environment


def check(volume, compiler, platform):
    root = volume / "runtime-work" / (platform + "-gnu")
    runtime = root / "cxx"
    windows = platform == "windows"
    target = (PROJECT / "recovered/llvm-all/inmemory/payload/root/targets/x86_64-windows-gnu"
              if windows else root / "target")
    library = target / ("lib" if windows else "usr/lib")
    command = [str(compiler), "c++", "-compile-target", "x86_64-" + platform + "-gnu",
               "--sysroot=" + str(target), "-std=c++23", "-O3", "-nostdinc++",
               "-isystem", str(runtime / "include/c++/v1"),
               "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src", "-nostdlib"]
    if windows:
        command += ["-isystem", str(target / "include/any-windows-any"),
                    str(library / "crt2.obj"), "-Wl,--entry,mainCRTStartup",
                    "-Wl,--subsystem,console"]
    else:
        command += ["-D__GLIBC_MINOR__=17", str(library / "crt1.o"), str(library / "crtbegin.o")]
    command += [str(PROJECT / "tests/host-runtime.cpp"), "-Wl,--start-group"]
    command += [str(runtime / "lib" / name) for name in ("libc++.a", "libc++abi.a", "libunwind.a")]
    if windows:
        command += [str(path) for path in sorted(library.glob("*.lib"))]
    else:
        builtins = list((root / "global/o").glob("*/libcompiler_rt.a"))
        if len(builtins) != 1:
            raise ValueError("expected one compiler builtins archive")
        command += [str(builtins[0])] + [str(library / name) for name in
                   ("libc_nonshared.a", "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2")]
    command += ["-Wl,--end-group"]
    if not windows:
        command += [str(library / "crtend.o")]
    output = root / ("hosted-probe.exe" if windows else "hosted-probe")
    command += ["-Wl,-s", "-o", str(output)]
    subprocess.run(command, env=build_environment(volume), check=True)
    inspection = subprocess.check_output(
        [str(compiler), "readobj", "--coff-imports" if windows else "--needed-libs", str(output)], text=True)
    if windows:
        names = re.findall(r"^  Name: (.+)$", inspection, re.M)
        allowed = lambda name: name.lower() in {"kernel32.dll", "ntdll.dll", "ucrtbase.dll"} or \
            name.lower().startswith("api-ms-win-crt-")
    else:
        block = re.search(r"NeededLibraries \[\n(.*?)\n\]", inspection, re.S)
        names = block.group(1).split() if block else []
        allowed = lambda name: name in {"libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2"}
        versions = subprocess.check_output([str(compiler), "readelf", "--version-info", str(output)], text=True)
        if any(tuple(map(int, minor.split("."))) > (2, 17)
               for minor in re.findall(r"GLIBC_(\d+\.\d+(?:\.\d+)?)", versions)):
            raise ValueError("probe requires glibc newer than 2.17")
    if not names or not all(allowed(name) for name in names):
        raise ValueError("unexpected target dependencies: " + ", ".join(names))
    print(platform + ": linked hosted C++ probe; system dependencies: " + ", ".join(names))
    print("Destination-OS execution is still required: " + str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("linux", "windows"))
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    args = parser.parse_args()
    check(workspace(), args.compiler.absolute(), args.platform)
