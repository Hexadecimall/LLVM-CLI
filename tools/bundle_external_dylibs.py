#!/usr/bin/env python3
"""Bundle absolute non-system Mach-O dependencies and rewrite references."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from recovered_paths import LEGACY_HOME_PATH

PROJECT = Path(__file__).resolve().parent.parent
ROOT = PROJECT / "recovered/llvm-all/stage"
DEST = ROOT / "deps/lib/dylibs"
EXTERNAL_PREFIXES = ("/opt/homebrew/", "/usr/local/")
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) in MACHO_MAGICS
    except OSError:
        return False


def dependencies(path: Path) -> list[str]:
    result = subprocess.run(["otool", "-L", str(path)], text=True, capture_output=True)
    if result.returncode:
        return []
    return [
        line.strip().split()[0]
        for line in result.stdout.splitlines()
        if line.strip() and line[:1].isspace()
    ]


def dylib_ids(path: Path) -> set[str]:
    result = subprocess.run(["otool", "-D", str(path)], text=True, capture_output=True)
    if result.returncode:
        return set()
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.rstrip().endswith(":")
    }


def linked_dependencies(path: Path) -> list[str]:
    identities = dylib_ids(path)
    return [dependency for dependency in dependencies(path) if dependency not in identities]


def is_external(dependency: str) -> bool:
    return dependency.startswith(EXTERNAL_PREFIXES) or bool(
        LEGACY_HOME_PATH.match(dependency)
    )


def candidates() -> list[Path]:
    result: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if is_macho(path) and (os.access(path, os.X_OK) or path.suffix in {".dylib", ".so"}):
            if dependencies(path):
                result.append(path)
    return result


def destination(source: Path) -> Path:
    target = DEST / source.name
    if target.exists() and target.resolve() != source.resolve():
        if target.read_bytes() != source.read_bytes():
            raise RuntimeError(f"dylib basename collision: {source} and {target}")
    return target


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    work = candidates()
    copied: dict[str, Path] = {}

    for binary in work:
        for dependency in linked_dependencies(binary):
            if not is_external(dependency):
                continue
            source = Path(dependency).resolve()
            if not source.exists():
                raise RuntimeError(f"missing dependency {dependency} required by {binary}")
            target = destination(source)
            if dependency not in copied:
                if not target.exists():
                    shutil.copy2(source, target)
                copied[dependency] = target
                work.append(target)

    for binary in work:
        for dependency in linked_dependencies(binary):
            if not is_external(dependency):
                continue
            target = copied.get(dependency)
            if target is None:
                source = Path(dependency).resolve()
                target = destination(source)
            relative = os.path.relpath(target, binary.parent)
            replacement = "@loader_path/" + relative
            subprocess.run(
                ["install_name_tool", "-change", dependency, replacement, str(binary)],
                check=True,
            )

    for binary in set(copied.values()):
        if dylib_ids(binary):
            subprocess.run(
                ["install_name_tool", "-id", "@rpath/" + binary.name, str(binary)],
                check=True,
            )

    for binary in work:
        subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"bundled {len(set(copied.values()))} external dylibs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
