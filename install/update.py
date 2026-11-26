#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Update LLVM-CLI in process using HTTPS and streaming SHA-256 verification."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import struct
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler
import zlib


REPOSITORY = "https://github.com/Hexadecimall/LLVM-CLI"
BLOCK = 1024 * 1024
MAX_IMAGE = 8 * 1024**3


class HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise ValueError("release download redirected outside HTTPS")
        return super().redirect_request(request, response, code, message, headers, newurl)


def release_opener():
    context = ssl.create_default_context()
    certificates = Path("/__llvm/share/llvm-cli/cacert.pem")
    if certificates.is_file():
        context.load_verify_locations(cadata=certificates.read_text())
    opener = build_opener(HttpsRedirect(), HTTPSHandler(context=context))
    def fetch(url):
        if urlparse(url).scheme != "https":
            raise ValueError("release download requires HTTPS")
        return opener.open(Request(url, headers={"User-Agent": "LLVM-CLI-updater"}), timeout=60)
    return fetch


def host_platform():
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "x86_64": "x86_64",
                    "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not system or not architecture:
        raise ValueError("no native release for this operating system or CPU")
    if system == "linux" and architecture == "arm64":
        architecture = "aarch64"
    return system + "-" + architecture


def version(value):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("expected a release version such as v1.8.0")
    return tuple(map(int, value.lstrip("v").split(".")))


def manifest_image(text, host):
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] not in (["llvm-cli-release-v1"], ["llvm-cli-release-v2"]):
        raise ValueError("unsupported release manifest")
    rows = [r for r in lines[1:] if len(r) > 1 and r[1] == host]
    binaries = [r for r in rows if r[0] == "binary"]
    if len(binaries) != 1:
        raise ValueError("release has no unique native " + host + " binary")
    binary = binaries[0]
    compressed = lines[0][0].endswith("v2")
    if len(binary) != (6 if compressed else 5) or (compressed and binary[5] != "gzip"):
        raise ValueError("invalid binary record")
    def number(value, maximum):
        if not re.fullmatch(r"[0-9]+", value) or not 0 < int(value) <= maximum:
            raise ValueError("invalid size or part count")
        return int(value)
    def digest(value):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("invalid SHA-256")
        return value
    size = number(binary[2], MAX_IMAGE)
    expected = digest(binary[3])
    count = number(binary[4], 999)
    parts = [r for r in rows if r[0] == "part"]
    if len(parts) != count:
        raise ValueError("missing or duplicate release part")
    indexed = {}
    for row in parts:
        if len(row) != 5 or not re.fullmatch(r"[0-9]+", row[2]):
            raise ValueError("invalid release part")
        index = int(row[2])
        if index in indexed or not 0 <= index < count:
            raise ValueError("duplicate or invalid part index")
        indexed[index] = (number(row[3], 1024**3), digest(row[4]))
    return size, expected, compressed, [indexed[i] for i in range(count)]


def validate_image(path, host):
    with path.open("rb") as source:
        header = source.read(64)
        if host.startswith("darwin-"):
            cpu = 0x0100000C if host.endswith("arm64") else 0x01000007
            valid = len(header) >= 8 and header[:4] == b"\xcf\xfa\xed\xfe" and struct.unpack_from("<I", header, 4)[0] == cpu
        elif host.startswith("linux-"):
            cpu = 183 if host.endswith("aarch64") else 62
            valid = len(header) >= 20 and header[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", header, 18)[0] == cpu
        else:
            valid = False
            if len(header) == 64 and header[:2] == b"MZ":
                source.seek(struct.unpack_from("<I", header, 60)[0])
                pe = source.read(6)
                cpu = 0xAA64 if host.endswith("arm64") else 0x8664
                valid = len(pe) == 6 and pe[:4] == b"PE\0\0" and struct.unpack_from("<H", pe, 4)[0] == cpu
    if not valid:
        raise ValueError("release image does not match the native host format")


def download_image(fetch, base, host, manifest, destination):
    size, expected, compressed, parts = manifest
    full = hashlib.sha256()
    written = 0
    decoder = zlib.decompressobj(31) if compressed else None
    with destination.open("xb") as output:
        def write(data):
            nonlocal written
            written += len(data)
            if written > size:
                raise ValueError("release expands beyond its declared size")
            output.write(data)
            full.update(data)
        for index, (part_size, part_hash) in enumerate(parts):
            print(f"Downloading part {index + 1}/{len(parts)}", flush=True)
            digest = hashlib.sha256()
            received = 0
            with fetch(f"{base}/llvm-cli-{host}.part{index:03d}") as response:
                while block := response.read(min(BLOCK, part_size - received + 1)):
                    received += len(block)
                    if received > part_size:
                        raise ValueError("release part exceeds its declared size")
                    digest.update(block)
                    if decoder:
                        while block:
                            write(decoder.decompress(block, BLOCK))
                            block = decoder.unconsumed_tail
                        if decoder.unused_data:
                            raise ValueError("trailing data in compressed release")
                    else:
                        write(block)
            if received != part_size or digest.hexdigest() != part_hash:
                raise ValueError("release part size or SHA-256 mismatch")
        if decoder and not decoder.eof:
            raise ValueError("truncated compressed release")
        if written != size or full.hexdigest() != expected:
            raise ValueError("release binary size or SHA-256 mismatch")
        output.flush()
        os.fsync(output.fileno())
    validate_image(destination, host)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check without installing")
    parser.add_argument("--version", default="latest", help="release tag (default: latest)")
    parser.add_argument("--executable", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--current-version", required=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    current = version(args.current_version)
    fetch = release_opener()
    tag = args.version
    if tag == "latest":
        with fetch("https://api.github.com/repos/Hexadecimall/LLVM-CLI/releases/latest") as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("release metadata is too large")
        tag = json.loads(data)["tag_name"]
    available = version(tag)
    if available <= current:
        print(f"LLVM-CLI {args.current_version} is current; release is {tag}.")
        return 0
    host = host_platform()
    base = REPOSITORY + "/releases/download/" + tag
    with fetch(base + "/llvm-cli-manifest-v1.txt") as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("release manifest is too large")
    manifest = manifest_image(data.decode("ascii"), host)
    print(f"LLVM-CLI {tag} is available for {host}.")
    if args.check:
        return 0
    if os.name == "nt":
        raise ValueError("in-place Windows updates are not implemented; use install.ps1")
    installed = Path(args.executable).resolve(strict=True)
    if shutil.disk_usage(installed.parent).free < manifest[0] + BLOCK:
        raise ValueError("not enough free space beside the installed executable")
    original = installed.stat()
    with tempfile.TemporaryDirectory(prefix=".llvm-cli-update-", dir=installed.parent) as scratch:
        candidate = Path(scratch) / "llvm"
        download_image(fetch, base, host, manifest, candidate)
        candidate.chmod(original.st_mode & 0o777)
        if installed.stat() != original:
            raise ValueError("installed executable changed during the download; retry")
        os.replace(candidate, installed)
    print(f"Updated to {tag}: {installed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, zlib.error) as error:
        print(f"LLVM-CLI update: {error}", file=sys.stderr)
        raise SystemExit(1)
