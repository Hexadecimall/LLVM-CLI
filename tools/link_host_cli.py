#!/usr/bin/env python3
"""Link native Linux and Windows CLI candidates from the shared host workspace."""

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
from urllib.request import urlopen

from macos_build import PROJECT, workspace, build_environment
from rename_entry import rename_entry

ZSTD_VERSION = "1.5.7"
ZSTD_SHA256 = "37d7284556b20954e56e1ca85b80226768902e2edabd3b649e9e72c0c9012ee3"


def compression_source(volume):
    source = volume / "runtime-work" / ("zstd-" + ZSTD_VERSION)
    if (source / "lib/zstd.h").is_file():
        return source / "lib"
    archive = volume / "downloads" / ("zstd-" + ZSTD_VERSION + ".tar.gz")
    if not archive.exists():
        pending = archive.with_suffix(".next")
        url = "https://codeload.github.com/facebook/zstd/tar.gz/refs/tags/v" + ZSTD_VERSION
        with urlopen(url, timeout=60) as response, pending.open("xb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(pending.read_bytes()).hexdigest() != ZSTD_SHA256:
            raise ValueError("zstd download checksum mismatch")
        os.replace(pending, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ZSTD_SHA256:
        raise ValueError("retained zstd archive checksum mismatch")
    with tarfile.open(archive) as upstream:
        for member in upstream.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("unexpected path or link in zstd source archive")
        upstream.extractall(source.parent)
    return source / "lib"

def reuse(source, destination):
    if source.is_symlink():
        if destination.is_symlink() and os.readlink(destination) == os.readlink(source):
            return
    elif destination.is_file() and os.path.samefile(source, destination):
        return
    elif destination.is_file():
        old, current = destination.stat(), source.stat()
        if (old.st_size, old.st_mtime_ns) == (current.st_size, current.st_mtime_ns):
            return
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".next")
    pending.unlink(missing_ok=True)
    if source.is_symlink():
        pending.symlink_to(os.readlink(source))
    else:
        try:
            os.link(source, pending)
        except OSError:
            shutil.copy2(source, pending)
    os.replace(pending, destination)


def stage_tree(source, destination):
    for path in sorted(source.rglob("*")):
        if path.is_file() or path.is_symlink():
            reuse(path, destination / path.relative_to(source))


def core_tools(build):
    definitions = build / "tools/llvm-driver/LLVMDriverTools.def"
    if not definitions.exists():
        definitions = next(build.rglob("LLVMDriverTools.def"))
    return re.findall(r'^LLVM_DRIVER_TOOL\("([^"]+)"', definitions.read_text(), re.M)


def stage_payload(volume, build, output, packer, run):
    payload = output / "payload"
    retained = PROJECT / "recovered/llvm-all/inmemory/payload/root"
    overlay = volume / "overlay/root"
    for targets in (retained / "targets", overlay / "targets"):
        stage_tree(targets, payload / "root/targets")
    stage_tree(overlay / "lib/clang/23", payload / "root/lib/clang/23")
    stage_tree(overlay / "include/c++", payload / "root/include/c++")
    for name in ("libc++.a", "libc++abi.a", "libunwind.a"):
        reuse(overlay / "lib" / name, payload / "root/lib" / name)
    for system in ("linux", "windows"):
        runtime = volume / "runtime-work" / (system + "-gnu")
        target = payload / "root/targets" / ("x86_64-" + system + "-gnu")
        for name in ("libc++.a", "libc++abi.a", "libunwind.a"):
            reuse(runtime / "cxx/lib" / name, target / "lib" / name)
        stage_tree(runtime / "cxx/include/c++/v1", target / "include/c++")
        if system == "linux":
            headers = runtime / "target/usr/include"
            for path in headers.rglob("*"):
                if path.is_file():
                    reuse(path.resolve(), target / "include/gnu" / path.relative_to(headers))
            library = runtime / "target/usr/lib"
            for name in ("crt1.o", "crtbegin.o", "crtend.o", "libc_nonshared.a",
                         "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1"):
                reuse(library / name, target / "lib" / name)
            builtins = list((runtime / "global/o").glob("*/libcompiler_rt.a"))
            if len(builtins) != 1:
                raise RuntimeError("expected one prepared GNU compiler builtins archive")
            reuse(builtins[0], target / "lib/libcompiler_rt.a")
    aliases = payload / "aliases"
    aliases.mkdir(parents=True, exist_ok=True)
    for name in core_tools(build):
        (aliases / name).write_text("llvm")
    for name in ("ar", "nm", "objcopy", "objdump", "ranlib", "readelf", "readobj", "strip",
                 "size", "lipo", "lib", "dlltool", "rc", "windres", "addr2line", "symbolizer"):
        if (aliases / name).exists():
            (aliases / ("llvm-" + name)).write_text("llvm")
    pack = output / "LLVM.pack"
    pending = output / "LLVM.next.pack"
    run([packer, payload, pending])
    os.replace(pending, pack)
    return pack


def compiler_commands(build, compiler):
    # The configured link recipe preserves all the runtime flags and host tool
    # dependencies that CMake selected. Paths remain relative to build/tools.
    recipe = build / "tools/CMakeFiles/llvm-driver.dir/link.txt"
    commands = [shlex.split(line) for line in recipe.read_text().splitlines() if line.strip()]
    link = commands[-1]
    if link[:2] != [str(compiler), "c++"]:
        raise RuntimeError("the LLVM driver link recipe needs the configured LLVM-CLI")
    flags = []
    for option in ("-compile-target", "-isystem"):
        for index, value in enumerate(link[:-1]):
            if value == option:
                flags.extend(link[index:index + 2])
    flags.extend(token for token in link if token.startswith(("--sysroot=", "-D__GLIBC_MINOR__", "-ffile-prefix-map=")))
    return flags, link


def link_cli(volume, platform, compiler, prepare_only, refresh_pack=False):
    host = volume / "hosts" / (platform + "-x86_64")
    build = host / "build"
    output = host / "image"
    output.mkdir(exist_ok=True)
    shared = volume / "hosts/shared-image"
    shared.mkdir(exist_ok=True)
    flags, link = compiler_commands(build, compiler)
    environment = build_environment(volume)
    compression = compression_source(volume)
    bzip = PROJECT / "recovered/llvm-all/deps-src/bzip2-1.0.8"

    with (host / "image.log").open("a") as log:
        def run(command, cwd=None):
            subprocess.run(list(map(str, command)), cwd=cwd, env=environment,
                           stdout=log, stderr=subprocess.STDOUT, check=True)

        with (shared / "pack.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pack = shared / "LLVM.pack"
            if refresh_pack or not pack.is_file():
                pack = stage_payload(volume, build, shared, volume / "tools/llvm-pack", run)
        objects = []
        c_flags = flags + ["-O3", "-flto=thin", "-DZSTD_DISABLE_ASM=1",
                           "-I" + str(compression), "-I" + str(bzip)]
        c_sources = sorted((compression / "common").glob("*.c"))
        c_sources += sorted((compression / "decompress").glob("*.c"))
        c_sources += [bzip / (name + ".c") for name in
                      ("blocksort", "huffman", "crctable", "randtable", "compress", "decompress", "bzlib")]
        for path in c_sources + [PROJECT / "tools" / name for name in
                                 ("llvm-memory.cpp", "native-wrappers.cpp", "incremental-build.cpp")]:
            obj = output / (path.stem + ".o")
            cpp = path.suffix == ".cpp"
            cpp_flags = ["-nostdinc++", "-std=c++23", "-fvisibility=hidden"] if cpp else []
            run([compiler, "c++" if cpp else "cc", *c_flags, *cpp_flags, "-c", path, "-o", obj])
            objects.append(str(obj))
        if prepare_only:
            return
        main = next((build / "tools/CMakeFiles/llvm-driver.dir").rglob("llvm-driver.cpp.o" if platform == "linux"
                                                                       else "llvm-driver.cpp.obj"))
        core = output / ("core-entry.obj" if platform == "windows" else "core-entry.o")
        rename_entry(main, core, "LLVM_llvm_main", compiler, compiler,
                     symbol_prefix="", objcopy_args=("objcopy",))
        if platform == "linux":
            packed = output / "LLVM.pack.o"
            # The relative binary input name defines the ELF symbols.
            run([compiler, "objcopy", "-I", "binary", "-O", "elf64-x86-64", "-B", "i386:x86-64",
                 "--rename-section", ".data=.rodata,alloc,load,readonly,data,contents", pack.name, packed],
                cwd=shared)
            objects.append(str(packed))
            exported = ("LLVMEnumerateEmbeddedFiles", "LLVMHasEmbeddedTool", "LLVMExecutablePath",
                        "LLVMRunEmbeddedTool", "open", "read", "pread", "lseek", "close", "fstat", "stat", "fcntl")
            objects.extend("-Wl,--export-dynamic-symbol=" + name for name in exported)
            link = [str(core) if token.endswith("/llvm-driver.cpp.o") else token for token in link]
        else:
            objects.append("-L" + str(volume / "runtime-work/windows-gnu/imports"))
            resource = output / "LLVM.rc"
            resource.write_text('1 RCDATA "' + pack.as_posix() + '"\n')
            packed = output / "LLVM.res"
            run([compiler, "rc", "/fo", packed, resource])
            objects.append(str(packed))
            response = main.parents[1] / "objects1.rsp"
            if not response.exists():
                response = build / "tools/CMakeFiles/llvm-driver.dir/objects1.rsp"
            inputs = shlex.split(response.read_text())
            inputs = [str(core) if entry.endswith("/llvm-driver.cpp.obj") else entry for entry in inputs]
            archive = output / "core.a"
            archive_args = output / "objects.rsp"
            archive_args.write_text("\n".join(shlex.quote(entry) for entry in inputs) + "\n")
            run([compiler, "ar", "rcs", archive, "@" + str(archive_args)], cwd=build / "tools")
            link = [str(archive) if token == "CMakeFiles/llvm-driver.dir/objects.a" else token for token in link]
            link = [token for token in link if not token.startswith("-Wl,--out-implib,")]
        destination = output / ("LLVM.exe" if platform == "windows" else "LLVM")
        candidate = destination.with_name(destination.stem + ".next" + destination.suffix)
        link[link.index("-o") + 1] = str(candidate)
        if not any(token.startswith("-Wl,--thinlto-cache-dir=") for token in link):
            objects.extend(["-Wl,--thinlto-cache-dir=" + str(output / "thinlto"),
                            "-Wl,--thinlto-cache-policy=cache_size_bytes=384m"])
        run([*link[:2], *objects, "-Wl,-s", *link[2:]], cwd=build / "tools")
        os.replace(candidate, destination)
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("linux", "windows"))
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--refresh-pack", action="store_true")
    args = parser.parse_args()
    link_cli(workspace(), args.platform, args.compiler.absolute(), args.prepare_only, args.refresh_pack)
