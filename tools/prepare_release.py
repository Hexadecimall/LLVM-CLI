#!/usr/bin/env python3
"""Split audited host binaries into GitHub-sized, SHA-256-verified assets."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import struct
import sys
import zlib

from audit_public import check_binary, check_source


CHUNK_SIZE = 1024 * 1024 * 1024
GZIP_CHUNK_SIZE = 256 * 1024 * 1024
COPY_SIZE = 8 * 1024 * 1024
PLATFORMS = {
    "darwin-arm64",
    "linux-x86_64",
    "linux-aarch64",
    "windows-x86_64",
    "windows-arm64",
}


def artifact(value: str) -> tuple[str, Path]:
    platform, separator, name = value.partition("=")
    if not separator or platform not in PLATFORMS or not name:
        raise argparse.ArgumentTypeError(
            "expected PLATFORM=PATH; supported platforms: " + ", ".join(sorted(PLATFORMS))
        )
    return platform, Path(name).resolve()


def validate_host_format(platform: str, source: Path) -> None:
    if not source.is_file():
        raise ValueError(f"missing host binary: {source}")
    with source.open("rb") as image:
        header = image.read(64)
        if platform == "darwin-arm64":
            valid = (
                header[:4] == b"\xcf\xfa\xed\xfe"
                and len(header) >= 8
                and struct.unpack_from("<I", header, 4)[0] == 0x0100000C
            )
        elif platform.startswith("linux-"):
            machine = 62 if platform == "linux-x86_64" else 183
            valid = (
                header[:5] == b"\x7fELF\x02"
                and len(header) >= 20
                and struct.unpack_from("<H", header, 18)[0] == machine
            )
        else:
            machine = 0x8664 if platform == "windows-x86_64" else 0xAA64
            valid = False
            if header[:2] == b"MZ" and len(header) >= 64:
                image.seek(struct.unpack_from("<I", header, 60)[0])
                pe_header = image.read(6)
                valid = (
                    pe_header[:4] == b"PE\0\0"
                    and len(pe_header) == 6
                    and struct.unpack_from("<H", pe_header, 4)[0] == machine
                )
    if not valid:
        raise ValueError(f"{source} is not a native {platform} executable")


def split_binary(platform: str, source: Path, destination: Path,
                 chunk_size: int = CHUNK_SIZE) -> list[str]:
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError(f"missing or empty host binary: {source}")
    if chunk_size < 1 or chunk_size >= 2 * 1024 * 1024 * 1024:
        raise ValueError("chunk size must be below GitHub's 2 GiB asset limit")

    total_size = source.stat().st_size
    part_count = (total_size + chunk_size - 1) // chunk_size
    if part_count > 999:
        raise ValueError("more than 999 parts are not supported")
    base = f"llvm-cli-{platform}"
    full_hash = hashlib.sha256()
    lines = []

    with source.open("rb") as input_file:
        for index in range(part_count):
            part_name = f"{base}.part{index:03d}"
            part_path = destination / part_name
            part_hash = hashlib.sha256()
            remaining = min(chunk_size, total_size - index * chunk_size)
            part_size = remaining
            # Exclusive creation prevents a rerun from silently replacing an asset.
            with part_path.open("xb") as output_file:
                while remaining:
                    block = input_file.read(min(COPY_SIZE, remaining))
                    if not block:
                        raise OSError(f"source changed during packaging: {source}")
                    output_file.write(block)
                    part_hash.update(block)
                    full_hash.update(block)
                    remaining -= len(block)
            lines.append(
                f"part {platform} {index} {part_size} {part_hash.hexdigest()}"
            )
        if input_file.read(1):
            raise OSError(f"source grew during packaging: {source}")

    return [
        f"binary {platform} {total_size} {full_hash.hexdigest()} {part_count}",
        *lines,
    ]


def split_gzip_binary(platform: str, source: Path, destination: Path,
                      chunk_size: int = GZIP_CHUNK_SIZE) -> list[str]:
    """Compress once, split the byte stream, and retain the final binary hash."""
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError(f"missing or empty host binary: {source}")
    if chunk_size < 1 or chunk_size >= 2 * 1024 * 1024 * 1024:
        raise ValueError("chunk size must be below GitHub's 2 GiB asset limit")

    total_size = source.stat().st_size
    full_hash = hashlib.sha256()
    lines = []
    output_file = None
    part_hash = hashlib.sha256()
    part_size = 0

    def finish_part() -> None:
        nonlocal output_file, part_hash, part_size
        if output_file is None:
            return
        output_file.close()
        lines.append(
            f"part {platform} {len(lines)} {part_size} {part_hash.hexdigest()}"
        )
        output_file = None
        part_hash = hashlib.sha256()
        part_size = 0

    def write_compressed(data: bytes) -> None:
        nonlocal output_file, part_size
        offset = 0
        while offset < len(data):
            if output_file is None:
                if len(lines) >= 999:
                    raise ValueError("more than 999 parts are not supported")
                part_name = f"llvm-cli-{platform}.part{len(lines):03d}"
                output_file = (destination / part_name).open("xb")
            block = data[offset:offset + chunk_size - part_size]
            output_file.write(block)
            part_hash.update(block)
            part_size += len(block)
            offset += len(block)
            if part_size == chunk_size:
                finish_part()

    compressor = zlib.compressobj(level=6, wbits=31)
    try:
        with source.open("rb") as input_file:
            remaining = total_size
            while remaining:
                block = input_file.read(min(COPY_SIZE, remaining))
                if not block:
                    raise OSError(f"source changed during packaging: {source}")
                full_hash.update(block)
                write_compressed(compressor.compress(block))
                remaining -= len(block)
            if input_file.read(1):
                raise OSError(f"source grew during packaging: {source}")
        write_compressed(compressor.flush())
    finally:
        finish_part()
    return [
        f"binary {platform} {total_size} {full_hash.hexdigest()} "
        f"{len(lines)} gzip",
        *lines,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="empty directory for release assets")
    parser.add_argument("--binary", type=artifact, action="append", required=True,
                        metavar="PLATFORM=PATH",
                        help="one verified native LLVM-CLI executable")
    parser.add_argument("--gzip", action="store_true",
                        help="emit v2 gzip-compressed parts (smaller downloads)")
    args = parser.parse_args()

    platforms = [platform for platform, _ in args.binary]
    if len(platforms) != len(set(platforms)):
        parser.error("each platform may appear only once")
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error("output directory must be empty")
    for platform, source in args.binary:
        try:
            validate_host_format(platform, source)
        except ValueError as error:
            parser.error(str(error))

    findings = check_source()
    for _, source in args.binary:
        if source.is_file():
            findings.extend(check_binary(source))
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = ["llvm-cli-release-v2" if args.gzip else "llvm-cli-release-v1"]
    for platform, source in args.binary:
        manifest.extend((split_gzip_binary if args.gzip else split_binary)(
            platform, source, output_dir))
    (output_dir / "llvm-cli-manifest-v1.txt").write_text(
        "\n".join(manifest) + "\n", encoding="ascii"
    )
    print(f"Release assets ready in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
