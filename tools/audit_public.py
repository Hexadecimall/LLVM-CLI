#!/usr/bin/env python3
"""Check public source and optional release images for local identifiers."""

from pathlib import Path
import argparse
import os
import re
import sys


PROJECT = Path(__file__).resolve().parent.parent
EXCLUDED = {"recovered", "dependency-bundles", "dependency-objects",
            "validation", "host-build", "release-assets", "build",
            "__pycache__", ".git"}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".md",
                   ".txt", ".f90", ".sh", ".ps1", ".json", ".yml",
                   ".yaml", ".toml"}
HOME = Path.home()
USERNAME = HOME.name.encode()
SOURCE_PATTERNS = (
    re.compile(rb"/(?:Users|home)/[^/\s]+/(?:\.cache|Projects|Developer)/"),
    re.compile(rb"[A-Za-z]:\\Users\\[^\\\s]+\\(?:\.cache|Projects|Developer)\\"),
    re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})"),
)


def source_paths():
    for directory, subdirectories, names in os.walk(PROJECT):
        subdirectories[:] = [name for name in subdirectories if name not in EXCLUDED]
        for name in names:
            path = Path(directory) / name
            if path.suffix in SOURCE_SUFFIXES or name in {"LICENSE", ".gitignore", "COPYING"}:
                yield path


def check_source() -> list[str]:
    problems = []
    for path in source_paths():
        # Upstream license texts may legitimately name their copyright holders.
        is_upstream_notice = "licenses" in path.relative_to(PROJECT).parts
        data = path.read_bytes()
        if not is_upstream_notice and USERNAME and USERNAME in data:
            problems.append(f"{path.relative_to(PROJECT)}: local account name")
        if not is_upstream_notice:
            for pattern in SOURCE_PATTERNS:
                if pattern.search(data):
                    problems.append(f"{path.relative_to(PROJECT)}: private path or credential pattern")
    return problems


def check_binary(path: Path) -> list[str]:
    patterns = [os.fsencode(HOME), os.fsencode(PROJECT), USERNAME]
    patterns = [value for value in patterns if value]
    carry = b""
    with path.open("rb") as image:
        while chunk := image.read(8 * 1024 * 1024):
            data = carry + chunk
            for pattern in patterns:
                if pattern in data:
                    return [f"{path}: contains a local home, project, or account path"]
            for pattern in SOURCE_PATTERNS:
                if pattern.search(data):
                    return [f"{path}: contains a private path or credential pattern"]
            carry = data[-512:]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", nargs="?", type=Path,
                        help="optional release binary to inspect")
    args = parser.parse_args()
    problems = check_source()
    if args.binary:
        problems.extend(check_binary(args.binary))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("Public-path and credential-pattern audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
