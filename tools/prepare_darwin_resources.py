#!/usr/bin/env python3
"""Reuse Zig's Darwin headers and text stubs as an embedded target resource."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def stage(zig_lib: Path, overlay: Path):
    headers = zig_lib / "libc/include/any-darwin-any"
    stubs = zig_lib / "libc/darwin"
    for required in (headers / "stdio.h", stubs / "libSystem.tbd",
                     stubs / "SDKSettings.json"):
        if not required.is_file():
            raise ValueError("incomplete Zig Darwin resources: " + str(required))
    destination = overlay / "root/targets/macos"
    sources = [(p, Path("usr/include") / p.relative_to(headers))
               for p in sorted(headers.rglob("*")) if p.is_file()]
    sources += [(stubs / "libSystem.tbd", Path("usr/lib/libSystem.tbd"))]
    additions = Path(__file__).resolve().parent.parent / "resources/darwin"
    sources += [(p, p.relative_to(additions)) for p in sorted(additions.rglob("*")) if p.is_file()]
    hashes = {}
    for source, relative in sources:
        data = source.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            shutil.copyfile(source, target)
        hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    # Zig only needs MinimalDisplayName. Clang additionally requires platform,
    # deployment limits, and supported triples to accept SDKSettings.json.
    upstream_metadata = (stubs / "SDKSettings.json").read_bytes()
    version = json.loads(upstream_metadata)["MinimalDisplayName"]
    settings = {
        "Version": version, "MinimalDisplayName": version,
        "DisplayName": "macOS " + version, "CanonicalName": "macosx" + version,
        "MaximumDeploymentTarget": version.split(".")[0] + ".99",
        "SupportedTargets": {"macosx": {
            "Archs": ["arm64", "x86_64"], "LLVMTargetTripleVendor": "apple",
            "LLVMTargetTripleSys": "macosx", "SystemPrefix": "",
        }},
    }
    data = (json.dumps(settings, separators=(",", ":")) + "\n").encode()
    (destination / "SDKSettings.json").write_bytes(data)
    hashes["SDKSettings.json"] = hashlib.sha256(data).hexdigest()
    record = overlay / "root/share/llvm-cli/darwin-resources.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "source": "https://github.com/ziglang/zig/tree/0.17.0/lib/libc",
        "description": "Unmodified Darwin headers and libSystem text stubs with generated Clang metadata; no framework SDK",
        "upstream_metadata_sha256": hashlib.sha256(upstream_metadata).hexdigest(),
        "sha256": hashes,
    }, indent=2) + "\n")
    print(f"Staged {len(sources)} Darwin resources ({sum(p.stat().st_size for p, _ in sources)} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig-lib", required=True, type=Path)
    parser.add_argument("--overlay", required=True, type=Path)
    args = parser.parse_args()
    stage(args.zig_lib, args.overlay)
