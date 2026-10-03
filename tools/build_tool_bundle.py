#!/usr/bin/env python3
"""Relink one LLVM executable target as an in-memory-loadable Mach-O bundle."""

from pathlib import Path
import argparse
import shlex
import shutil
import subprocess
from recovered_paths import relocate

PROJECT = Path(__file__).resolve().parent.parent
BUILD = PROJECT / "recovered/llvm-all/build"
OBJCOPY = BUILD / "bin/llvm-objcopy"
STATIC_CXX = [
    BUILD / "lib/libc++.a",
    BUILD / "lib/libc++abi.a",
    BUILD / "lib/libunwind.a",
]
LIBLLDB_LINK = BUILD / "tools/lldb/source/API/CMakeFiles/liblldb.dir/link.txt"
LLDB_PYTHON_ENTRY = PROJECT / "tools/lldb-python-entry.cpp"
def relocated(token: str) -> str:
    return relocate(token, PROJECT).replace(
        "/private/tmp/llvm-all-wrapper", str(PROJECT / "tools"))


def append_static_link_inputs(command: list[str], link_file: Path) -> None:
    root = link_file.parent.parent.parent
    tokens = shlex.split(link_file.read_text())
    framework_next = False
    for raw_token in tokens[1:]:
        token = relocated(raw_token)
        if framework_next:
            command.extend(["-framework", token])
            framework_next = False
            continue
        if token == "-framework":
            framework_next = True
            continue
        if token.endswith(".dylib") or token.startswith("@rpath/"):
            continue
        if token.endswith((".o", ".a", ".tbd")):
            path = Path(token)
            command.append(str(path if path.is_absolute() else (root / path).resolve()))
        elif token.startswith(("-l", "-F")):
            command.append(token)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool")
    parser.add_argument("link_file", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    link_file = args.link_file.resolve()
    root = link_file.parent.parent.parent
    tokens = shlex.split(link_file.read_text())
    flat_namespace = args.tool in {"lldb", "lldb-dap", "lldb-mcp"}
    rewritten = [tokens[0], "-bundle"]
    if flat_namespace:
        rewritten.extend(["-flat_namespace", "-undefined", "suppress"])
    else:
        rewritten.extend(["-undefined", "dynamic_lookup"])
    rewritten.append("-nostdlib++")
    skip = False
    renamed_main = False
    work = args.output.parent / (args.output.name + ".objects")
    work.mkdir(parents=True, exist_ok=True)

    for index, raw_token in enumerate(tokens[1:]):
        token = relocated(raw_token)
        if skip:
            skip = False
            continue
        if token == "-o":
            skip = True
            continue
        if token in {"-rdynamic", "-pie"}:
            continue
        if token.endswith(".o"):
            source = Path(token)
            if not source.is_absolute():
                source = (root / source).resolve()
            nm = subprocess.run(["nm", str(source)], text=True, capture_output=True, check=True)
            if any(line.endswith(" T _main") for line in nm.stdout.splitlines()):
                target = work / source.name
                shutil.copy2(source, target)
                symbol = "_LLVM_" + args.tool.replace("-", "_") + "_main"
                subprocess.run(
                    [str(OBJCOPY), "--redefine-sym", f"_main={symbol}", str(target)],
                    check=True,
                )
                rewritten.append(str(target))
                renamed_main = True
            else:
                rewritten.append(str(source))
            continue
        if args.tool in {"lldb", "lldb-dap", "lldb-mcp"} and token.endswith(".dylib"):
            continue
        if token.endswith((".a", ".dylib", ".tbd", ".so")) and not Path(token).is_absolute():
            rewritten.append(str((root / token).resolve()))
        else:
            rewritten.append(token)

    if not renamed_main:
        raise RuntimeError(f"no main symbol found for {args.tool}")
    if args.tool == "lldb":
        entry_object = work / "lldb-python-entry.o"
        subprocess.run(
            [tokens[0], "-std=c++17", "-fvisibility=hidden", "-c",
             str(LLDB_PYTHON_ENTRY), "-o", str(entry_object)],
            check=True,
        )
        rewritten.append(str(entry_object))
    if args.tool in {"lldb", "lldb-dap", "lldb-mcp"}:
        append_static_link_inputs(rewritten, LIBLLDB_LINK)
    if not flat_namespace:
        rewritten.extend(map(str, STATIC_CXX))
    rewritten.extend(["-Wl,-no_warn_duplicate_libraries", "-o", str(args.output)])
    return subprocess.run(rewritten, cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
