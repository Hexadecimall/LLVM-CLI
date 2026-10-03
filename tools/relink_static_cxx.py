#!/usr/bin/env python3
"""Relink built LLVM Mach-O deliverables with the bundled static C++ runtime."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent.parent
BUILD = PROJECT / "recovered/llvm-all/build"
STATIC_LIBS = [
    BUILD / "lib/libc++.a",
    BUILD / "lib/libc++abi.a",
    BUILD / "lib/libunwind.a",
]


def output_from(tokens: list[str], link_file: Path) -> tuple[Path, Path] | None:
    for index, token in enumerate(tokens[:-1]):
        if token == "-o":
            command_root = link_file.parent.parent.parent
            candidate = (command_root / tokens[index + 1]).resolve()
            return candidate, command_root
    return None


def is_deliverable(path: Path) -> bool:
    try:
        relative = path.relative_to(BUILD)
    except ValueError:
        return False
    return relative.parts[0] in {"bin", "lib"} and path.exists()


def uses_dynamic_libcxx(path: Path) -> bool:
    result = subprocess.run(
        ["otool", "-L", str(path)], text=True, capture_output=True
    )
    return result.returncode == 0 and "/usr/lib/libc++.1.dylib" in result.stdout


def commands() -> list[tuple[Path, Path, list[str]]]:
    found: dict[Path, tuple[Path, Path, list[str]]] = {}
    for link_file in BUILD.rglob("link.txt"):
        text = link_file.read_text().strip()
        if not text:
            continue
        tokens = shlex.split(text)
        resolved = output_from(tokens, link_file)
        if not resolved:
            continue
        output, command_root = resolved
        if is_deliverable(output) and uses_dynamic_libcxx(output):
            found[output] = (output, command_root, tokens)
    return sorted(found.values(), key=lambda item: str(item[0]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--match", action="append", default=[])
    parser.add_argument("--restore-compiler-rt", action="store_true")
    args = parser.parse_args()

    missing = [str(path) for path in STATIC_LIBS if not path.exists()]
    if missing:
        raise SystemExit("missing static runtime archives: " + ", ".join(missing))

    if args.restore_compiler_rt:
        runtime_dir = BUILD / "lib/clang/23/lib/darwin"
        selected = []
        for link_file in BUILD.rglob("link.txt"):
            tokens = shlex.split(link_file.read_text().strip())
            resolved = output_from(tokens, link_file)
            if not resolved:
                continue
            output, command_root = resolved
            if output.parent == runtime_dir and output.name.endswith("_dynamic.dylib"):
                selected.append((output, command_root, tokens))
        selected.sort(key=lambda item: str(item[0]))
        for number, (output, cwd, tokens) in enumerate(selected, 1):
            print(f"[{number}/{len(selected)}] restore {output.name}", flush=True)
            result = subprocess.run(tokens, cwd=cwd)
            if result.returncode:
                return result.returncode
        return 0

    selected = commands()
    if args.match:
        selected = [
            command
            for command in selected
            if any(pattern in str(command[0]) for pattern in args.match)
        ]
    if args.list:
        for output, _, _ in selected:
            print(output)
        print(f"{len(selected)} link commands", file=sys.stderr)
        return 0

    failures = 0
    for number, (output, cwd, tokens) in enumerate(selected, 1):
        print(f"[{number}/{len(selected)}] {output.relative_to(BUILD)}", flush=True)
        command = [tokens[0], "-nostdlib++", *tokens[1:], *map(str, STATIC_LIBS)]
        result = subprocess.run(command, cwd=cwd)
        if result.returncode:
            failures += 1
            print(f"FAILED ({result.returncode}): {output}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
