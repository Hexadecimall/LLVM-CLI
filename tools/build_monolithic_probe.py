#!/usr/bin/env python3
"""Construct a direct-linked multicall probe from existing LLVM link graphs."""

from pathlib import Path
import shlex
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parent.parent
BUILD = PROJECT / "recovered/llvm-all/build"
OUT = PROJECT / "recovered/llvm-all/monolithic-probe"
OBJCOPY = BUILD / "bin/llvm-objcopy"
TOOLS = {
    "llvm": BUILD / "tools/CMakeFiles/llvm-driver.dir/link.txt",
    "flang-23": BUILD / "tools/flang/tools/flang-driver/CMakeFiles/flang.dir/link.txt",
    "mlir-opt": BUILD / "tools/mlir/tools/mlir-opt/CMakeFiles/mlir-opt.dir/link.txt",
    "llvm-bolt": BUILD / "tools/bolt/tools/driver/CMakeFiles/llvm-bolt.dir/link.txt",
    "opt": BUILD / "tools/opt/CMakeFiles/opt.dir/link.txt",
}


def command_root(link: Path) -> Path:
    return link.parent.parent.parent


def absolute(token: str, root: Path) -> str:
    path = Path(token)
    return str(path if path.is_absolute() else (root / path).resolve())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    objects: list[str] = []
    libraries: list[str] = []
    seen_objects: set[str] = set()
    seen_libraries: set[str] = set()
    framework_next = False

    for tool, link in TOOLS.items():
        root = command_root(link)
        tokens = shlex.split(link.read_text())
        for token in tokens[1:]:
            if token == "-framework":
                framework_next = True
                continue
            if framework_next:
                pair = "-framework\0" + token
                if pair not in seen_libraries:
                    libraries.extend(["-framework", token])
                    seen_libraries.add(pair)
                framework_next = False
                continue
            if token.endswith(".o"):
                source = Path(absolute(token, root))
                key = str(source)
                if key in seen_objects:
                    continue
                seen_objects.add(key)
                target = OUT / f"{tool}-{len(objects)}.o"
                shutil.copy2(source, target)
                nm = subprocess.run(["nm", str(target)], text=True, capture_output=True, check=True)
                if any(line.endswith(" T _main") for line in nm.stdout.splitlines()):
                    renamed = "_LLVM_" + tool.replace("-", "_") + "_main"
                    subprocess.run([str(OBJCOPY), "--redefine-sym", f"_main={renamed}", str(target)], check=True)
                objects.append(str(target))
                continue
            is_library = token.endswith((".a", ".dylib", ".tbd", ".so"))
            if is_library:
                token = absolute(token, root)
            if is_library or token.startswith(("-l", "-F")):
                if token not in seen_libraries:
                    libraries.append(token)
                    seen_libraries.add(token)

    dispatcher = OUT / "dispatch.o"
    subprocess.run([
        "/usr/local/bin/c++", "-std=c++20", "-O2", "-c",
        "/private/tmp/llvm-monolithic-dispatch.cpp", "-o", str(dispatcher),
    ], check=True)
    response = OUT / "link.rsp"
    response.write_text("\n".join(shlex.quote(item) for item in [
        str(dispatcher), *objects, *libraries,
        "-nostdlib++", str(BUILD / "lib/libc++.a"),
        str(BUILD / "lib/libc++abi.a"), str(BUILD / "lib/libunwind.a"),
        "-Wl,-no_warn_duplicate_libraries", "-Wl,-headerpad_max_install_names",
        "-o", str(OUT / "LLVM"),
    ]))
    print(f"{len(objects)} objects, {len(libraries)} library arguments")
    return subprocess.run(["/usr/local/bin/c++", "@" + str(response)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
