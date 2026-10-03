#!/usr/bin/env python3
"""Pack and link the final single-file LLVM command from the durable payload."""

from pathlib import Path
import os
import mmap
import shlex
import shutil
import struct
import subprocess
from recovered_paths import recorded_home_paths, relocate

PROJECT = Path(__file__).resolve().parent.parent
TOOLS = PROJECT / "tools"
WORK = PROJECT / "recovered/llvm-all"
BUILD = WORK / "build"
IMAGE = WORK / "inmemory"
PAYLOAD = IMAGE / "payload"
PACK = IMAGE / "LLVM-memory.next.pack"
OUTPUT = IMAGE / "LLVM.next"
CORE_LINK = BUILD / "tools/CMakeFiles/llvm-driver.dir/link.txt"
CORE_MAIN = IMAGE / "bundles/llvm.bundle.objects/llvm-driver.cpp.o"
def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def relocated(value: str) -> str:
    return relocate(value, PROJECT)


def scrub_private_paths(binary: Path) -> bool:
    recorded = CORE_LINK.read_text() if CORE_LINK.exists() else ""
    prefixes = {str(PROJECT).encode(), str(Path.home()).encode()}
    prefixes.update(path.encode() for path in recorded_home_paths(recorded))
    changed = False
    with binary.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as image:
        for prefix in sorted(prefixes, key=len, reverse=True):
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
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or path.stat().st_size == 0:
                continue
            if scrub_private_paths(path):
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
        str(BUILD / "lib/libc++.a"),
        str(BUILD / "lib/libc++abi.a"),
        str(BUILD / "lib/libunwind.a"),
        str(WORK / "deps/lib/libbz2.a"),
        f"-Wl,-sectcreate,__LLVM,__pack,{pack}",
        "-Wl,-no_warn_duplicate_libraries", "-o", str(output),
    ])
    return command


def main() -> int:
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
    container = TOOLS / "llvm-memory.o"
    wrappers = TOOLS / "native-wrappers.o"
    builder = TOOLS / "incremental-build.o"

    run([
        compiler, "-std=c++23", "-O3", "-fvisibility=hidden",
        f"-I{TOOLS}", f"-I{zstd_include}", "-c",
        str(TOOLS / "llvm-memory.cpp"), "-o", str(container),
    ])
    run([
        compiler, "-std=c++23", "-O3", "-fvisibility=hidden",
        f"-I{TOOLS}", f"-I{WORK / 'deps/include'}", "-c",
        str(TOOLS / "native-wrappers.cpp"), "-o", str(wrappers),
    ])
    run([
        compiler, "-std=c++23", "-O3", "-fvisibility=hidden",
        f"-I{TOOLS}", "-c", str(TOOLS / "incremental-build.cpp"),
        "-o", str(builder),
    ])
    changed_payloads = scrub_payload_paths(PAYLOAD)
    resign_payload_machos(PAYLOAD, changed_payloads)
    run([str(TOOLS / "llvm-pack"), str(PAYLOAD), str(PACK)])
    run(core_link_command(container, wrappers, builder, PACK, OUTPUT))
    scrub_private_paths(OUTPUT)
    run(["codesign", "-s", "-", "--force", str(OUTPUT)])
    os.replace(OUTPUT, IMAGE / "LLVM")
    PACK.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
