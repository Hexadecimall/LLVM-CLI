#!/usr/bin/env python3
"""Prepare a small GNU/Linux target runtime using retained resource sources.

LLVM-CLI compiles the probes, compiler-rt startup objects, and C++ runtime.
An installed Zig generates glibc import stubs and its minimal C startup archive;
it never compiles LLVM project sources. Build-only headers are symlinked.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from macos_build import RELEASE, PROJECT, workspace, build_environment


def run(command, environment, log):
    subprocess.run(list(map(str, command)), env=environment, stdout=log,
                   stderr=subprocess.STDOUT, check=True)


def shared_file(source, destination):
    source = source.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and os.path.samefile(source, destination):
        return
    temporary = destination.with_name(destination.name + ".next")
    temporary.unlink(missing_ok=True)
    # The installed resource tree can reside on another filesystem. Symlinks
    # reuse it during builds; published payloads must materialize target data.
    try:
        os.link(source, temporary)
    except OSError:
        temporary.symlink_to(source)
    os.replace(temporary, destination)


def prepare(volume, compiler, zig, zig_lib, jobs, build_cxx):
    root = volume / "runtime-work/linux-gnu"
    root.mkdir(parents=True, exist_ok=True)
    environment = build_environment(volume)
    environment.update(ZIG_GLOBAL_CACHE_DIR=str(root / "global"),
                       ZIG_LOCAL_CACHE_DIR=str(root / "local"))
    target = root / "target"
    includes = zig_lib / "libc/include"
    header_sets = [includes / name for name in
                   ("x86-linux-gnu", "generic-glibc", "x86-linux-any", "any-linux-any")]
    for directory in header_sets:
        if not directory.is_dir():
            raise ValueError("missing target headers: " + str(directory))
    # Architecture overrides generic libc, which overrides kernel headers.
    for directory in reversed(header_sets):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                shared_file(path, target / "usr/include" / path.relative_to(directory))
    flags = ["-compile-target", "x86_64-linux-gnu", "--sysroot=" + str(target),
             "-D__GLIBC_MINOR__=17", "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"]
    with (root / "bootstrap.log").open("a") as log:
        probe = root / "probe.c"
        probe.write_text("int main(void) { return 0; }\n")
        run([compiler, "cc", *flags, "-c", probe, "-o", root / "probe.o"], environment, log)
        run([zig, "cc", "-target", "x86_64-linux-gnu.2.17", root / "probe.o",
             "-o", root / "probe"], environment, log)
        cache = root / "global/o"
        # Zig's content-addressed outputs are discovered by unique file name,
        # never by a machine-specific cache key.
        for name in ("crt1.o", "libc_nonshared.a", "libc.so.6", "libm.so.6",
                     "libpthread.so.0", "libdl.so.2", "librt.so.1", "libresolv.so.2"):
            matches = list(cache.glob("*/" + name))
            if len(matches) != 1:
                raise ValueError("expected one generated runtime resource: " + name)
            shared_file(matches[0], target / "usr/lib" / name)
            if ".so." in name:
                shared_file(matches[0], target / "usr/lib" / (name.split(".so.")[0] + ".so"))
        for name in ("crtbegin", "crtend"):
            source = volume / "source/compiler-rt/lib/builtins" / (name + ".c")
            run([compiler, "cc", *flags, "-O3", "-fPIC", "-DCRT_HAS_INITFINI_ARRAY",
                 "-c", source, "-o", target / "usr/lib" / (name + ".o")], environment, log)
    if not build_cxx:
        return target
    build_cxx_runtime(volume, root, compiler, flags, jobs)
    (root / "resources.json").write_text(json.dumps({
        "llvm_version": RELEASE["version"], "target": "x86_64-linux-gnu",
        "glibc_minimum": "2.17", "project_compiler": "LLVM-CLI",
        "runtime_resource_generator": "Zig (glibc imports and C startup only)",
        "headers": "symlinked build inputs; materialize before packaging",
    }, indent=2) + "\n")
    return target


def build_cxx_runtime(volume, root, compiler, flags, jobs, system="Linux",
                      triple="x86_64-linux-gnu"):
    environment = build_environment(volume)
    runtime = root / "cxx"
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("llvm-ar", "llvm-ranlib"):
        (tools / name).unlink(missing_ok=True)
        (tools / name).symlink_to(compiler)
    common = shlex.join(flags)
    command = ["cmake", "-S", volume / "source/runtimes", "-B", runtime,
               "-G", "Unix Makefiles", "-DCMAKE_SYSTEM_NAME=" + system,
               "-DCMAKE_SYSTEM_PROCESSOR=x86_64", "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_C_COMPILER=" + str(compiler), "-DCMAKE_C_COMPILER_ARG1=cc",
               "-DCMAKE_CXX_COMPILER=" + str(compiler), "-DCMAKE_CXX_COMPILER_ARG1=c++",
               "-DCMAKE_ASM_COMPILER=" + str(compiler), "-DCMAKE_ASM_COMPILER_ARG1=cc",
               "-DCMAKE_C_FLAGS=" + common, "-DCMAKE_CXX_FLAGS=" + common,
               "-DCMAKE_ASM_FLAGS=" + common, "-DCMAKE_AR=" + str(tools / "llvm-ar"),
               "-DCMAKE_RANLIB=" + str(tools / "llvm-ranlib"),
               "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
               "-DCMAKE_INSTALL_PREFIX=/__llvm/targets/" + triple,
               "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind",
               "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF", "-DLLVM_ENABLE_LTO=OFF",
               "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DLIBCXX_ENABLE_SHARED=OFF",
               "-DLIBCXXABI_ENABLE_SHARED=OFF", "-DLIBUNWIND_ENABLE_SHARED=OFF",
               "-DLIBCXX_INCLUDE_TESTS=OFF", "-DLIBCXXABI_INCLUDE_TESTS=OFF",
               "-DLIBUNWIND_INCLUDE_TESTS=OFF", "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
               "-DLIBCXX_INCLUDE_DOCS=OFF", "-DLIBCXXABI_INCLUDE_DOCS=OFF",
               "-DLIBCXX_HARDENING_MODE=none", "-DLIBCXX_ABI_VERSION=1",
               "-DLIBCXX_ABI_NAMESPACE=__1", "-DLIBCXX_ENABLE_STATIC_ABI_LIBRARY=OFF",
               "-DLIBCXXABI_USE_LLVM_UNWINDER=ON", "-DLIBUNWIND_USE_COMPILER_RT=ON"]
    command += ["-DLLVM_APPEND_VC_REV=OFF", "-DCMAKE_DISABLE_FIND_PACKAGE_Git=ON"]
    with (root / "cxx-build.log").open("a") as log:
        run(command, environment, log)
        run(["cmake", "--build", runtime, "--target", "cxx", "cxxabi", "unwind",
             "--parallel", str(jobs)], environment, log)
    return runtime


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--zig-lib", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--c-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    print(prepare(workspace(), args.compiler.absolute(), args.zig.absolute(),
                  args.zig_lib.absolute(), args.jobs, not args.c_only))
