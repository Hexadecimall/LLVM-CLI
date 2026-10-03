#!/usr/bin/env python3
"""Build every linked LLVM-family command as an isolated in-memory bundle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import shlex
import subprocess

PROJECT = Path(__file__).resolve().parent.parent
BUILD = PROJECT / "recovered/llvm-all/build"
OUTPUT = PROJECT / "recovered/llvm-all/inmemory/bundles"
BUILDER = PROJECT / "tools/build_tool_bundle.py"


def linked_output(link_file: Path) -> Path | None:
    tokens = shlex.split(link_file.read_text())
    for index, token in enumerate(tokens[:-1]):
        if token == "-o":
            return (link_file.parent.parent.parent / tokens[index + 1]).resolve()
    return None


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError:
        return False
    return magic in {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    }


def build(item: tuple[str, Path]) -> tuple[str, int, str]:
    name, link_file = item
    output = OUTPUT / (name + ".bundle")
    result = subprocess.run(
        ["python3", str(BUILDER), name, str(link_file), str(output)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return name, result.returncode, result.stdout


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    links: dict[str, Path] = {}
    for link_file in BUILD.rglob("link.txt"):
        output = linked_output(link_file)
        if not output or output.parent != BUILD / "bin":
            continue
        if output.is_symlink() or not output.is_file() or not is_macho(output):
            continue
        links[output.name] = link_file

    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(build, item): item[0] for item in sorted(links.items())}
        for number, future in enumerate(as_completed(futures), 1):
            name, status, output = future.result()
            print(f"[{number}/{len(futures)}] {name}: {'ok' if status == 0 else 'FAILED'}", flush=True)
            if status:
                failures[name] = output

    manifest = {
        "bundles": sorted(name for name in links if name not in failures),
        "failures": failures,
    }
    (OUTPUT.parent / "build-manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"built {len(manifest['bundles'])}; failed {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
