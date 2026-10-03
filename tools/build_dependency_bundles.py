#!/usr/bin/env python3
"""Build runnable dependency commands as in-memory Mach-O bundles."""

from pathlib import Path
import os
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parent.parent
WORK = PROJECT / "recovered/llvm-all"
BUILD = WORK / "build"
DEPS = WORK / "deps"
SOURCES = WORK / "deps-src"
OBJECTS = PROJECT / "dependency-objects"
OUTPUT = PROJECT / "dependency-bundles"
OBJCOPY = BUILD / "bin/llvm-objcopy"
STATIC_CXX = [
    BUILD / "lib/libc++.a",
    BUILD / "lib/libc++abi.a",
    BUILD / "lib/libunwind.a",
]


def renamed_main(name: str, source: Path) -> Path:
    target = OBJECTS / f"{name}-{source.name}"
    shutil.copy2(source, target)
    symbol = f"_LLVM_{name.replace('-', '_')}_main"
    subprocess.run(
        [str(OBJCOPY), "--redefine-sym", f"_main={symbol}", str(target)],
        check=True,
    )
    return target


def link(name: str, inputs: list[Path | str], *, cxx: bool = False) -> None:
    compiler = "clang++" if cxx else "clang"
    command = [compiler, "-bundle", "-flat_namespace", "-undefined", "suppress"]
    if cxx:
        command.append("-nostdlib++")
    command.extend(map(str, inputs))
    if cxx:
        command.extend(map(str, STATIC_CXX))
    command.extend(["-o", str(OUTPUT / f"{name}.bundle")])
    subprocess.run(command, check=True)


def build_swig() -> None:
    root = WORK / "build-swig/Source"
    main = root / "Modules/swigmain.o"
    objects = sorted(root.rglob("*.o"))
    objects.remove(main)
    objects.insert(0, renamed_main("swig", main))
    configured = os.environ.get("PCRE2_STATIC_LIB")
    if configured:
        pcre2 = Path(configured)
    else:
        libdir = subprocess.check_output(
            ["pkg-config", "--variable=libdir", "libpcre2-8"], text=True
        ).strip()
        pcre2 = Path(libdir) / "libpcre2-8.a"
    if not pcre2.is_file():
        raise RuntimeError(f"PCRE2 static library not found: {pcre2}")
    link("swig", [*objects, pcre2], cxx=True)


def main() -> int:
    OBJECTS.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    build_swig()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
