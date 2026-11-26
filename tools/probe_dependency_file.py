#!/usr/bin/env python3
"""Exercise the patched Clang depfile writer against the retained native SDK."""

import subprocess
from pathlib import Path

from macos_build import PROJECT, workspace, build_environment


def probe(volume, compiler):
    output = volume / "dependency-probe"
    output.mkdir(exist_ok=True)
    common = [str(compiler), "c++", "-std=c++17", "-O3", "-I/__llvm/include",
              "-I/__llvm/deps/include", "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"]
    libraries = list(map(str, (volume / "overlay/root/lib").glob("libclang*.a")))
    libraries += subprocess.check_output([str(volume / "native-sdk/bin/llvm-config"),
                                          "--link-static", "--libfiles", "all"], text=True).split()
    deps = ["/__llvm/deps/lib/" + name for name in
            ("libz.a", "libzstd.a", "libxml2.a", "libiconv.a", "libcharset.a")]
    environment = build_environment(volume)
    with (output / "build.log").open("w") as log:
        commands = [common + ["-c", str(volume / "source/clang/lib/Frontend/DependencyFile.cpp"),
                               "-o", str(output / "DependencyFile.o")],
                    common + [str(PROJECT / "tests/dependency-file.cpp"), str(output / "DependencyFile.o"),
                              *libraries, *deps, "-Wl,-exported_symbol,_main",
                              "-Wl,-exported_symbol,_LLVMExecutablePath", "-Wl,-x",
                              "-o", str(output / "dependency-file")]]
        for command in commands:
            subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    for mode in ("embedded", "standalone", "missing"):
        result = subprocess.check_output([str(output / "dependency-file"), mode], text=True)
        if mode == "embedded":
            if "/__llvm/" in result or result.count("/compiler\\ path/llvm\\#test") != 2:
                raise RuntimeError("embedded dependency deduplication failed")
        elif "/__llvm/one.h" not in result or "/compiler" in result:
            raise RuntimeError("standalone or missing-header dependency behavior changed")
        if "user\\ header.h" not in result:
            raise RuntimeError("ordinary header dependency was lost")
        print(mode + ": dependency writer passed")


if __name__ == "__main__":
    import argparse
    import shutil
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    args = parser.parse_args()
    probe(workspace(), args.compiler.absolute())
