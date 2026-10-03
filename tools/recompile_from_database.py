#!/usr/bin/env python3
"""Recompile one relocated LLVM object using its recovered compile command."""

from pathlib import Path
import json
import shlex
import subprocess
import sys
from recovered_paths import relocate

PROJECT = Path(__file__).resolve().parent.parent
WORK = PROJECT / "recovered/llvm-all"
BUILD = WORK / "build"
def relocated(value: str) -> str:
    return relocate(value, PROJECT)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: recompile_from_database.py <source-suffix>")
    commands = json.loads((BUILD / "compile_commands.json").read_text())
    matches = [item for item in commands if item["file"].endswith(sys.argv[1])]
    if len(matches) != 1:
        raise SystemExit(f"expected one compile command, found {len(matches)}")
    item = matches[0]
    command = [relocated(token) for token in shlex.split(item["command"])]
    without_pch: list[str] = []
    index = 0
    while index < len(command):
        if (command[index:index + 2] == ["-Xclang", "-include-pch"] or
                command[index:index + 2] == ["-Xclang", "-include"]):
            index += 4
            continue
        without_pch.append(command[index])
        index += 1
    command = without_pch
    return subprocess.run(command, cwd=relocated(item["directory"])).returncode


if __name__ == "__main__":
    raise SystemExit(main())
