#!/usr/bin/env python3
"""Make Xcode ThinLTO archives usable by upstream linkers, retaining members."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile


def archive_members(path):
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ValueError("expected a regular archive: " + str(path))
        names = b""
        while True:
            header = stream.read(60)
            if not header:
                return
            if len(header) != 60 or header[58:] != b"`\n":
                raise ValueError("invalid archive member header")
            size = int(header[48:58])
            data = stream.read(size)
            if len(data) != size:
                raise ValueError("truncated archive member")
            if size & 1:
                stream.read(1)
            name = header[:16].decode("ascii").strip()
            if name == "//":
                names = data
                continue
            if name in ("/", "/SYM64/"):
                continue
            if name.startswith("#1/"):
                length = int(name[3:])
                name = data[:length].rstrip(b"\0").decode()
                data = data[length:]
            elif name.startswith("/") and name[1:].isdigit():
                offset = int(name[1:])
                end = names.find(b"/\n", offset)
                if end < 0:
                    raise ValueError("invalid archive name table")
                name = names[offset:end].decode()
            else:
                name = name.rstrip("/")
            if name.startswith("__.SYMDEF"):
                continue
            if not name or Path(name).name != name:
                raise ValueError("unsafe archive member name")
            yield name, data


def materialize(source, destination, compiler, jobs=2):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return
    with tempfile.TemporaryDirectory(prefix="archive-", dir=destination.parent) as scratch:
        root = Path(scratch)

        def convert(item):
            index, member = item
            name, data = member
            directory = root / str(index)
            directory.mkdir()
            output = directory / name
            if data[:4] in (b"BC\xc0\xde", b"\xde\xc0\x17\x0b"):
                bitcode = directory / "input.bc"
                bitcode.write_bytes(data)
                subprocess.run([str(compiler), "-x", "ir", "-c", "-O3",
                                "-mmacosx-version-min=27.0", str(bitcode),
                                "-o", str(output)], check=True)
                bitcode.unlink()
            else:
                output.write_bytes(data)
            return output

        with ThreadPoolExecutor(max_workers=jobs) as executor:
            objects = list(executor.map(convert, enumerate(archive_members(source))))
        if not objects:
            # Empty LLVM component archives are valid build-system placeholders.
            staged = root / "empty.a"
            staged.write_bytes(b"!<arch>\n")
        else:
            staged = root / "native.a"
            subprocess.run(["xcrun", "libtool", "-static", "-o", str(staged),
                            *map(str, objects)], check=True)
        staged.replace(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    materialize(args.source.resolve(), args.destination.resolve(), compiler, args.jobs)


if __name__ == "__main__":
    main()
