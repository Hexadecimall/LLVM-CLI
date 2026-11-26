#!/usr/bin/env python3
"""Verify LLVM's in-process object loader on macOS using retained native SDK inputs.

This is a portability experiment, not a Linux or Windows host edition.
It embeds one precompiled object and LLVM's ORC runtime in a single executable.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

from macos_build import PROJECT, workspace, build_environment


def probe(volume, compiler):
    output = volume / "object-loader-probe"
    output.mkdir(exist_ok=True)
    sdk = volume / "native-sdk"
    (sdk / "bin").mkdir(parents=True, exist_ok=True)
    config = sdk / "bin/llvm-config"
    if not config.exists():
        os.link(volume / "build/bin/llvm-config", config)
    for name in ("lib", "include", "deps"):
        path = sdk / name
        if not path.exists():
            path.symlink_to(volume / "overlay/root" / name, target_is_directory=True)
    libraries = subprocess.check_output(
        [str(config), "--link-static", "--libfiles", "orcjit", "native"], text=True).split()
    common = [str(compiler), "c++", "-std=c++23", "-O3",
              "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"]
    deps = ["/__llvm/deps/lib/" + name for name in
            ("libz.a", "libzstd.a", "libxml2.a", "libiconv.a", "libcharset.a")]
    environment = build_environment(volume)
    with (output / "build.log").open("w") as log:
        commands = [
            [str(compiler), "lipo", "-thin", "arm64",
             str(volume / "compiler-rt/lib/darwin/liborc_rt_osx.a"),
             "-output", str(output / "orc-runtime.a")],
            [str(compiler), "strip", "-S", str(output / "orc-runtime.a")],
            common + ["-fPIC", "-c", str(PROJECT / "tools/probe_object_module.cpp"),
                      "-o", str(output / "module.o")],
            common + ["-flto=thin", "-I/__llvm/include", "-I/__llvm/deps/include",
                      str(PROJECT / "tools/probe_object_loader.cpp"), *libraries, *deps,
                      "-Wl,-x", "-Wl,-sectcreate,__LLVM,__orc," + str(output / "orc-runtime.a"),
                      "-Wl,-sectcreate,__LLVM,__module," + str(output / "module.o"),
                      "-o", str(output / "object-tool")],
        ]
        for command in commands:
            subprocess.run(command, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
    result = subprocess.run([str(output / "object-tool")], env={"PATH": "/nonexistent"},
                            text=True, capture_output=True, check=True)
    for marker in ("embedded constructor ran", "embedded destructor ran", "object entry returned 42"):
        if marker not in result.stdout:
            raise RuntimeError("missing lifecycle check: " + marker)
    print(result.stdout, end="")


def linux_probe(volume, compiler):
    root = volume / "runtime-work/linux-gnu"
    output = root / "object-loader-probe"
    output.mkdir(exist_ok=True)
    target = root / "target"
    cxx = root / "cxx"
    library = target / "usr/lib"
    common = [str(compiler), "c++", "-std=c++23", "-O3", "-compile-target", "x86_64-linux-gnu",
              "--sysroot=" + str(target), "-D__GLIBC_MINOR__=17", "-nostdinc++",
              "-isystem", str(cxx / "include/c++/v1"),
              "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"]
    names = subprocess.check_output([str(volume / "native-sdk/bin/llvm-config"),
                                     "--link-static", "--libnames", "orcjit", "x86codegen"], text=True).split()
    libraries = [str(volume / "hosts/linux-x86_64/build/lib" / name) for name in names]
    if any(not Path(path).is_file() for path in libraries):
        raise RuntimeError("Linux loader libraries must finish building first")
    runtime_archive = output / "orc-runtime.a"
    if not runtime_archive.exists():
        os.link(root / "orc/orc-runtime.a", runtime_archive)
    environment = build_environment(volume)
    with (output / "build.log").open("w") as log:
        subprocess.run(common + ["-fPIC", "-c", str(PROJECT / "tools/probe_object_module.cpp"),
                                  "-o", str(output / "module.o")], env=environment,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        for name in ("module.o", "orc-runtime.a"):
            subprocess.run([str(compiler), "objcopy", "-I", "binary", "-O", "elf64-x86-64",
                            "-B", "i386:x86-64", "--rename-section",
                            ".data=.rodata,alloc,load,readonly,data,contents", name, name + ".data.o"],
                           cwd=output, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
        builtins = list((root / "global/o").glob("*/libcompiler_rt.a"))
        if len(builtins) != 1:
            raise RuntimeError("expected one GNU compiler builtins archive")
        command = common + ["-flto=thin", "-I" + str(volume / "hosts/linux-x86_64/build/include"),
                            "-I" + str(volume / "source/llvm/include"), "-nostdlib",
                            str(library / "crt1.o"), str(library / "crtbegin.o"),
                            str(PROJECT / "tools/probe_object_loader.cpp"),
                            str(output / "module.o.data.o"), str(output / "orc-runtime.a.data.o"),
                            "-Wl,--export-dynamic", "-Wl,--start-group", *libraries]
        command += [str(cxx / "lib" / name) for name in ("libc++.a", "libc++abi.a", "libunwind.a")]
        command += [str(builtins[0])] + [str(library / name) for name in
                   ("libc_nonshared.a", "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2")]
        command += ["-Wl,--end-group", str(library / "crtend.o"), "-Wl,-s", "-o", str(output / "object-tool")]
        subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    print("Linked Linux embedded-object probe; destination execution is still required: " + str(output / "object-tool"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    parser.add_argument("--platform", choices=("macos", "linux"), default="macos")
    args = parser.parse_args()
    (linux_probe if args.platform == "linux" else probe)(workspace(), args.compiler.absolute())
