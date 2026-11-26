#!/usr/bin/env python3
"""Pack and link the final single-file LLVM command from the durable payload."""

from pathlib import Path
import argparse
import os
import mmap
import re
import shlex
import shutil
import struct
import subprocess
import sys
from recovered_paths import recorded_home_paths, relocate

PROJECT = Path(__file__).resolve().parent.parent
TOOLS = PROJECT / "tools"
WORK = PROJECT / "recovered/llvm-all"
BUILD = Path(os.environ.get("LLVM_CLI_NATIVE_BUILD_DIR", WORK / "build")).resolve()
IMAGE = Path(os.environ.get("LLVM_CLI_IMAGE_DIR", WORK / "inmemory")).resolve()
PAYLOAD = Path(os.environ.get("LLVM_CLI_PAYLOAD_DIR", WORK / "inmemory/payload")).resolve()
OVERLAY = Path(os.environ["LLVM_CLI_OVERLAY_DIR"]).resolve() if "LLVM_CLI_OVERLAY_DIR" in os.environ else None
STDLIB = Path(os.environ.get("LLVM_CLI_STDLIB_DIR", BUILD / "lib")).resolve()
PACKER = Path(os.environ.get("LLVM_CLI_PACKER", TOOLS / "llvm-pack")).resolve()
PACK = IMAGE / "LLVM-memory.next.pack"
OUTPUT = IMAGE / "LLVM.next"
CORE_LINK = BUILD / "tools/CMakeFiles/llvm-driver.dir/link.txt"
CORE_MAIN = IMAGE / "bundles/llvm.bundle.objects/llvm-driver.cpp.o"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def relocated(value: str) -> str:
    return relocate(value, PROJECT)


def private_path_prefixes():
    recorded = CORE_LINK.read_text() if CORE_LINK.exists() else ""
    prefixes = {str(PROJECT).encode(), str(Path.home()).encode()}
    prefixes.update(path.encode() for path in recorded_home_paths(recorded))
    return sorted(prefixes, key=len, reverse=True)


def scrub_private_paths(binary: Path, prefixes=None) -> bool:
    if prefixes is None:
        prefixes = private_path_prefixes()
    changed = False
    with binary.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as image:
        for prefix in prefixes:
            replacement = (b"/__llvm" + b"/" * (len(prefix) - len(b"/__llvm"))) \
                if len(prefix) >= len(b"/__llvm") else b"/" * len(prefix)
            offset = 0
            while (offset := image.find(prefix, offset)) != -1:
                image[offset:offset + len(prefix)] = replacement
                changed = True
                offset += len(prefix)
    return changed


def scrub_payload_paths(root: Path) -> set[Path]:
    changed = set()
    prefixes = private_path_prefixes()
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or path.stat().st_size == 0:
                continue
            if scrub_private_paths(path, prefixes):
                changed.add(path)
    return changed


def resign_payload_machos(root: Path, changed: set[Path]) -> None:
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or path.stat().st_size < 16:
                continue
            with path.open("rb") as handle:
                header = handle.read(16)
            if header[:4] != b"\xcf\xfa\xed\xfe":
                continue
            file_type = struct.unpack_from("<I", header, 12)[0]
            if file_type in (2, 6, 8) and path in changed:
                run(["codesign", "-s", "-", "--force", str(path)])


def core_link_command(container: Path, wrappers: Path, builder: Path, pack: Path,
                      output: Path) -> list[str]:
    cache_percent = os.environ.get("LLVM_CLI_LTO_CACHE_PERCENT", "5")
    if cache_percent not in {"1", "2", "3", "4", "5"}:
        raise ValueError("LLVM_CLI_LTO_CACHE_PERCENT must be between 1 and 5")
    tokens = shlex.split(CORE_LINK.read_text())
    root = BUILD / "tools"
    exported = [
        "_LLVMEnumerateEmbeddedFiles", "_LLVMHasEmbeddedTool",
        "_LLVMExecutablePath", "_LLVMRunEmbeddedTool", "_open", "_read",
        "_pread", "_lseek", "_close", "_fstat", "_stat", "_fcntl",
    ]
    command = [tokens[0], "-nostdlib++"]
    command.extend(f"-Wl,-exported_symbol,{symbol}" for symbol in exported)
    command.extend([str(container), str(wrappers), str(builder)])
    skip = False
    for raw_token in tokens[1:]:
        token = relocated(raw_token)
        if skip:
            skip = False
            continue
        if token == "-o":
            skip = True
            continue
        if token.startswith("-Wl,-rpath,"):
            continue
        if token == "CoreFoundation":
            if command and command[-1] == "-framework":
                command.pop()
            continue
        if token.endswith("CMakeFiles/llvm-driver.dir/llvm-driver/llvm-driver.cpp.o"):
            command.append(str(CORE_MAIN))
            continue
        if token.endswith((".o", ".a", ".tbd")) and not Path(token).is_absolute():
            command.append(str((root / token).resolve()))
        else:
            command.append(token)
    command.extend([
        str(STDLIB / "libc++.a"),
        str(STDLIB / "libc++abi.a"),
        str(STDLIB / "libunwind.a"),
        str(WORK / "deps/lib/libbz2.a"),
        f"-Wl,-sectcreate,__LLVM,__pack,{pack}",
        "-Wl,-prune_interval_lto,60", "-Wl,-max_relative_cache_size_lto," + cache_percent,
        "-Wl,-x", "-Wl,-no_warn_duplicate_libraries", "-o", str(output),
    ])
    return command


def missing_build_inputs() -> list[Path]:
    """Check the preserved link closure before compiling or changing payloads."""
    required = [
        CORE_LINK, CORE_MAIN, PACKER,
        STDLIB / "libc++.a", STDLIB / "libc++abi.a",
        STDLIB / "libunwind.a", WORK / "deps/lib/libbz2.a",
    ]
    generated = {IMAGE / "objects" / name for name in (
        "llvm-memory.o", "native-wrappers.o", "incremental-build.o")}
    if CORE_LINK.is_file():
        command = core_link_command(
            IMAGE / "objects/llvm-memory.o",
            IMAGE / "objects/native-wrappers.o",
            IMAGE / "objects/incremental-build.o", PACK, OUTPUT)
        required.extend(Path(token) for token in command[1:]
                        if not token.startswith("-") and
                        token.endswith((".o", ".a", ".tbd", ".dylib", ".so")) and
                        Path(token) not in generated)
    missing = [path for path in dict.fromkeys(required) if not path.is_file()]
    for directory in (PAYLOAD / "bundles", PAYLOAD / "root",
                      WORK / "deps/include"):
        if not directory.is_dir():
            missing.append(directory)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="check native link inputs without changing files")
    parser.add_argument("--reuse-pack", action="store_true",
                        help="reuse a completed pack when payload resources have not changed")
    args = parser.parse_args()
    missing = missing_build_inputs()
    if missing:
        print("LLVM-CLI: native macOS build inputs are incomplete:", file=sys.stderr)
        for path in missing:
            display = path.relative_to(PROJECT) if path.is_relative_to(PROJECT) else path
            print(f"  missing: {display}", file=sys.stderr)
        print("Restore or regenerate the native LLVM build directory before "
              "relinking. See docs/building.md. No build was started.", file=sys.stderr)
        return 1
    if args.check:
        print("LLVM-CLI: native macOS link inputs are present")
        return 0

    compiler = os.environ.get("CXX") or shutil.which("c++")
    if not compiler:
        raise RuntimeError("C++ compiler not found; set CXX")
    zstd_include = os.environ.get("ZSTD_INCLUDE_DIR")
    if not zstd_include:
        zstd_include = subprocess.check_output(
            ["pkg-config", "--variable=includedir", "libzstd"], text=True
        ).strip()
    if not (Path(zstd_include) / "zstd.h").exists():
        raise RuntimeError("zstd.h not found; set ZSTD_INCLUDE_DIR")
    objects = IMAGE / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    container = objects / "llvm-memory.o"
    wrappers = objects / "native-wrappers.o"
    builder = objects / "incremental-build.o"

    version_flags = []
    common_flags = ["-flto=thin", "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"]
    common_flags.extend(token for token in shlex.split(CORE_LINK.read_text())
                        if token.startswith("-mmacosx-version-min="))
    version_header = (BUILD / "include/llvm/Config/llvm-config.h").read_text()
    match = re.search(r'^#define LLVM_VERSION_STRING "([0-9A-Za-z.+-]+)"',
                      version_header, re.MULTILINE)
    if not match:
        raise RuntimeError("cannot identify the LLVM version in the native build")
    version = match[1]
    if os.environ.get("LLVM_CLI_LLVM_VERSION", version) != version:
        raise RuntimeError("the requested LLVM version does not match the native build")
    version_flags.append('-DLLVM_CLI_LLVM_VERSION="' + version + '"')
    run([
        compiler, "-std=c++23", "-O3", "-fvisibility=hidden",
        *common_flags, *version_flags,
        f"-I{TOOLS}", f"-I{zstd_include}", "-c",
        str(TOOLS / "llvm-memory.cpp"), "-o", str(container),
    ])
    run([
        compiler, "-std=c++23", "-O3", "-fvisibility=hidden",
        *common_flags,
        f"-I{TOOLS}", f"-I{WORK / 'deps/include'}", "-c",
        str(TOOLS / "native-wrappers.cpp"), "-o", str(wrappers),
    ])
    run([
        compiler, "-std=c++23", "-O3", "-fvisibility=hidden",
        *common_flags,
        f"-I{TOOLS}", "-c", str(TOOLS / "incremental-build.cpp"),
        "-o", str(builder),
    ])
    if args.reuse_pack:
        with PACK.open("rb") as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b"LLVMPK02" or \
                struct.unpack_from("<II", header, 8)[0] != 2:
            raise RuntimeError("the retained payload pack is incomplete")
    else:
        mutable_payload = OVERLAY or PAYLOAD
        changed_payloads = scrub_payload_paths(mutable_payload)
        resign_payload_machos(mutable_payload, changed_payloads)
        run([str(PACKER), str(PAYLOAD), str(PACK)] + ([str(OVERLAY)] if OVERLAY else []))
    core = BUILD / "bin/llvm"
    core_size = core.stat().st_size if core.is_file() else 256 * 1024 ** 2
    required = PACK.stat().st_size + core_size + 256 * 1024 ** 2
    if shutil.disk_usage(IMAGE).free < required:
        raise RuntimeError("insufficient workspace space for final linking; "
                           "the completed payload pack was preserved")
    run(core_link_command(container, wrappers, builder, PACK, OUTPUT))
    PACK.unlink()
    scrub_private_paths(OUTPUT)
    run(["codesign", "-s", "-", "--force", str(OUTPUT)])
    os.replace(OUTPUT, IMAGE / "LLVM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
