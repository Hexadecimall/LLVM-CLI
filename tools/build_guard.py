#!/usr/bin/env python3
"""Run a project build with explicit disk limits and no temporary workspace."""

from pathlib import Path
import argparse
import os
import shutil
import signal
import subprocess
import sys
import time


PROJECT = Path(__file__).resolve().parent.parent
GIB = 1024 ** 3


def directory_size(path: Path) -> int:
    result = subprocess.run(["du", "-sk", str(path)], text=True,
                            capture_output=True)
    if not result.stdout.split():
        raise RuntimeError(result.stderr.strip() or "could not measure build directory")
    return int(result.stdout.split()[0]) * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--limit-gib", type=float, default=24)
    parser.add_argument("--min-free-gib", type=float, default=32)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(PROJECT) or directory == PROJECT:
        parser.error("build directory must be a subdirectory of LLVM-CLI")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing build command")

    process = subprocess.Popen(command, cwd=PROJECT, start_new_session=True)
    try:
        while process.poll() is None:
            size = directory_size(directory)
            free = shutil.disk_usage(PROJECT).free
            print(f"build guard: {size / GIB:.1f} GiB in build directory; "
                  f"{free / GIB:.1f} GiB free", flush=True)
            if size > args.limit_gib * GIB or free < args.min_free_gib * GIB:
                print("build guard: disk limit reached; stopping build",
                      file=sys.stderr, flush=True)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                return 75
            time.sleep(30)
    except KeyboardInterrupt:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        return 130
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
