#!/usr/bin/env python3
"""Cross-build LLVM's native-object runtime without another compiler toolchain."""

import argparse
from pathlib import Path
import re
import shutil

from macos_build import PROJECT, workspace, build_environment
from prepare_linux_runtime import run


def prepare(volume, compiler):
    root = volume / "runtime-work/linux-gnu"
    output = root / "orc"
    output.mkdir(exist_ok=True)
    source = volume / "source/compiler-rt/lib/orc"
    cmake = (source / "CMakeLists.txt").read_text()
    common = re.search(r"set\(ORC_COMMON_SOURCES\s+(.*?)\)", cmake, re.S)
    if not common:
        raise ValueError("upstream ORC source list not found")
    files = common.group(1).split() + ["elfnix_platform.cpp", "elfnix_tls.x86-64.S",
                                       "sysv_reenter.x86-64.S"]
    flags = ["-compile-target", "x86_64-linux-gnu", "--sysroot=" + str(root / "target"),
             "-D__GLIBC_MINOR__=17", "-O3", "-fPIC", "-fvisibility=hidden",
             "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src",
             "-I" + str(source.parent), "-I" + str(source.parent.parent / "include")]
    environment = build_environment(volume)
    objects = []
    with (output / "build.log").open("w") as log:
        for name in files:
            path = source / name
            if not path.is_file():
                raise ValueError("missing ORC runtime source: " + name)
            obj = output / (name + ".o")
            cpp = (["-std=c++17", "-nostdinc++", "-isystem", str(root / "cxx/include/c++/v1"),
                    "-fno-exceptions", "-fno-rtti"] if name.endswith(".cpp") else [])
            run([compiler, "c++" if cpp else "cc", *flags, *cpp, "-c", path, "-o", obj],
                environment, log)
            objects.append(obj)
        run([compiler, "ar", "rcs", output / "orc-runtime.a", *objects], environment, log)
    return output / "orc-runtime.a"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    args = parser.parse_args()
    print(prepare(workspace(), args.compiler.absolute()))
