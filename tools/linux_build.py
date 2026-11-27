#!/usr/bin/env python3
"""Cross-build the Linux or Windows LLVM core inside the shared disk cap.

Reuse the prepared LLVM source, native table generators, and target runtime.
"""

import argparse
from pathlib import Path
import shlex
import shutil
import subprocess

from macos_build import PROJECT, workspace, build_environment
from build_cache import restore_build_cache
from remap_embedded_dependencies import remap


def configure(volume, compiler, platform="linux"):
    windows = platform == "windows"
    triple = "x86_64-w64-windows-gnu" if windows else "x86_64-unknown-linux-gnu"
    runtime = volume / "runtime-work" / (platform + "-gnu")
    target = runtime / "target"
    cxx = runtime / "cxx"
    library = target / "usr/lib"
    builtins = list((runtime / "global/o").glob("*/libcompiler_rt.a"))
    if not (cxx / "lib/libc++.a").is_file() or (not windows and len(builtins) != 1):
        raise RuntimeError(f"prepare_{platform}_runtime.py must complete first")
    build = volume / "hosts" / (platform + "-x86_64") / "build"
    build.mkdir(parents=True, exist_ok=True)
    restore_build_cache(build)
    repaired = remap(build, compiler)
    if repaired:
        print(f"Repaired virtual header dependencies in {repaired} generated records", flush=True)
    tools = runtime / "tools"
    crt = [str(cxx / "lib" / name) for name in ("libc++.a", "libc++abi.a", "libunwind.a")]
    path_map = "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"
    compile_flags = [
        "-compile-target", "x86_64-linux-gnu", "--sysroot=" + str(target),
        "-D__GLIBC_MINOR__=17", path_map,
    ]
    if windows:
        target = PROJECT / "recovered/llvm-all/inmemory/payload/root/targets/x86_64-windows-gnu"
        library = target / "lib"
        compile_flags = ["-compile-target", "x86_64-windows-gnu", "--sysroot=" + str(target), path_map]
        startup = ["-nostdlib", str(library / "crt2.obj"), "-Wl,--entry,mainCRTStartup",
                   "-L" + str(runtime / "imports")]
        standard = ["-Wl,--start-group", *crt, *map(str, sorted(library.glob("*.lib"))), "-Wl,--end-group"]
        c_headers = ["-isystem", str(target / "include/any-windows-any")]
    else:
        startup = ["-nostdlib", str(library / "crt1.o"), str(library / "crtbegin.o")]
        standard = ["-Wl,--start-group", *crt, str(builtins[0]),
                    *[str(library / name) for name in
                      ("libc_nonshared.a", "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2")],
                    "-Wl,--end-group", str(library / "crtend.o")]
        c_headers = []
    cxx_flags = shlex.join([*compile_flags, "-nostdinc++", "-isystem", str(cxx / "include/c++/v1"), *c_headers])
    compile_flags = shlex.join([*compile_flags, *c_headers])
    startup, standard = shlex.join(startup), shlex.join(standard)
    command = ["cmake", "-S", volume / "source/llvm", "-B", build,
               "-G", "Unix Makefiles", "-DCMAKE_SYSTEM_NAME=" + platform.capitalize(),
               "-DCMAKE_SYSTEM_PROCESSOR=x86_64", "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_C_COMPILER=" + str(compiler), "-DCMAKE_C_COMPILER_ARG1=cc",
               "-DCMAKE_CXX_COMPILER=" + str(compiler), "-DCMAKE_CXX_COMPILER_ARG1=c++",
               "-DCMAKE_ASM_COMPILER=" + str(compiler), "-DCMAKE_ASM_COMPILER_ARG1=cc",
               "-DCMAKE_C_FLAGS=" + compile_flags, "-DCMAKE_CXX_FLAGS=" + cxx_flags,
               "-DCMAKE_ASM_FLAGS=" + compile_flags, "-DCMAKE_EXE_LINKER_FLAGS=" + startup,
               "-DCMAKE_C_STANDARD_LIBRARIES:STRING=" + standard,
               "-DCMAKE_CXX_STANDARD_LIBRARIES:STRING=" + standard,
               "-DCMAKE_AR=" + str(tools / "llvm-ar"),
               "-DCMAKE_RANLIB=" + str(tools / "llvm-ranlib"),
               "-DCMAKE_TRY_COMPILE_TARGET_TYPE=EXECUTABLE",
               "-DCMAKE_TRY_COMPILE_PLATFORM_VARIABLES=CMAKE_CXX_STANDARD_LIBRARIES;CMAKE_C_STANDARD_LIBRARIES",
               "-DCMAKE_INSTALL_PREFIX=/__llvm", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
               "-DLLVM_HOST_TRIPLE=" + triple, "-DLLVM_DEFAULT_TARGET_TRIPLE=" + triple,
               "-DLLVM_NATIVE_TOOL_DIR=" + str(volume / "build/bin"),
               "-DLLVM_ENABLE_PROJECTS=clang;lld", "-DLLVM_TARGETS_TO_BUILD=all",
               "-DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD=all", "-DLLVM_ENABLE_LTO=Thin",
               "-DLLVM_ENABLE_RTTI=ON", "-DLLVM_ENABLE_EH=OFF", "-DBUILD_SHARED_LIBS=OFF",
               "-DLLVM_BUILD_LLVM_DYLIB=OFF", "-DLLVM_LINK_LLVM_DYLIB=OFF",
               "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
               "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_INCLUDE_DOCS=OFF",
               "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF",
               "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_TERMINFO=OFF",
               "-DLLVM_ENABLE_LIBEDIT=OFF", "-DLLVM_TOOL_LLVM_DRIVER_BUILD=ON",
               "-DLLVM_APPEND_VC_REV=OFF", "-DCMAKE_DISABLE_FIND_PACKAGE_Git=ON",
               "-DLLVM_ENABLE_BACKTRACES=ON"]
    cache = build / "CMakeCache.txt"
    if cache.is_file() and ("CMAKE_TRY_COMPILE_TARGET_TYPE:UNINITIALIZED=STATIC_LIBRARY" in cache.read_text() or
                            "CMAKE_TRY_COMPILE_PLATFORM_VARIABLES:" not in cache.read_text()):
        # Static try-compiles accept declarations without checking target CRT
        # symbols (for example mallinfo2 on glibc 2.17). Repeat those checks.
        command += ["-U", "HAVE_*", "-U", "LLVM_LINKER_*"]
    environment = build_environment(volume)
    if windows:
        command += ["-DCMAKE_RC_COMPILER=" + str(volume / "build/bin/llvm-rc")]
    with (build.parent / "configure.log").open("a") as log:
        subprocess.run(list(map(str, command)), env=environment, stdout=log,
                       stderr=subprocess.STDOUT, check=True)
    return build, environment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--platform", choices=("linux", "windows"), default="linux")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--targets", nargs="+", default=["LLVMOrcJIT", "LLVMX86CodeGen"])
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    volume = workspace()
    build, environment = configure(volume, args.compiler.absolute(), args.platform)
    if args.build:
        with (build.parent / "build.log").open("a") as log:
            for target in args.targets:
                subprocess.run(["cmake", "--build", str(build), "--target", target,
                                "--parallel", str(args.jobs)], env=environment,
                               stdout=log, stderr=subprocess.STDOUT, check=True)
                remap(build, args.compiler.absolute())
    print(build)
