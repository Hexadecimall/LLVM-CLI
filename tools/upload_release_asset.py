#!/usr/bin/env python3
"""Stream a release asset using ghx credentials without storing the token."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_id", type=int)
    parser.add_argument("asset", type=Path)
    parser.add_argument("--repo", default="Hexadecimall/LLVM-CLI")
    args = parser.parse_args()

    asset = args.asset.resolve(strict=True)
    if not asset.is_file() or asset.stat().st_size == 0:
        parser.error("asset must be a nonempty regular file")
    if asset.stat().st_size >= 2 * 1024 * 1024 * 1024:
        parser.error("asset exceeds GitHub's 2 GiB release limit")
    if args.repo != "Hexadecimall/LLVM-CLI":
        parser.error("this uploader is restricted to the LLVM-CLI repository")

    token = subprocess.check_output(
        ["ghx", "auth", "token"], text=True, stderr=subprocess.DEVNULL
    ).strip()
    if not token:
        raise RuntimeError("ghx did not provide an authentication token")

    url = (
        f"https://uploads.github.com/repos/{args.repo}/releases/"
        f"{args.release_id}/assets?name={quote(asset.name)}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(asset.stat().st_size),
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "LLVM-CLI-release-uploader",
    }
    print(f"Uploading {asset.name} ({asset.stat().st_size} bytes)", flush=True)
    try:
        with asset.open("rb") as source:
            request = Request(url, data=source, headers=headers, method="POST")
            with urlopen(request, timeout=3600) as response:
                result = json.load(response)
                if response.status != 201:
                    raise RuntimeError(f"unexpected upload status {response.status}")
    except HTTPError as error:
        detail = error.read(1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub upload failed ({error.code}): {detail}") from None
    except URLError as error:
        raise RuntimeError(f"GitHub upload failed: {error.reason}") from None

    if result.get("name") != asset.name or result.get("size") != asset.stat().st_size:
        raise RuntimeError("GitHub returned a mismatched asset name or size")
    remote_digest = result.get("digest")
    local_digest = f"sha256:{sha256(asset)}"
    if remote_digest and remote_digest != local_digest:
        raise RuntimeError("GitHub returned a mismatched asset SHA-256 digest")
    print(f"Uploaded and verified {asset.name}: {local_digest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"release upload: {error}", file=sys.stderr)
        raise SystemExit(1)
