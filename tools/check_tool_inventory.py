#!/usr/bin/env python3
"""Reject a packaged LLVM-CLI that drops an existing command name."""

import argparse
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    required = set((PROJECT / "config/required-tools.txt").read_text().splitlines())
    result = subprocess.run([str(args.binary.resolve()), "list"],
                            text=True, capture_output=True, timeout=120)
    if result.returncode:
        print(result.stderr or result.stdout, file=sys.stderr)
        return 1
    available = set(result.stdout.splitlines())
    missing = sorted(required - available)
    if missing:
        print(f"LLVM-CLI: {len(missing)} required command names are missing:",
              file=sys.stderr)
        for name in missing:
            print("  " + name, file=sys.stderr)
        return 1
    print(f"LLVM-CLI: all {len(required)} existing command names are advertised "
          f"({len(available)} total)")
    print("Run compiler, linker, debugger, and embedded-resource smoke tests "
          "before installation; inventory alone does not prove functionality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
