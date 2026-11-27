#!/usr/bin/env python3
"""Run locally linked host candidates on the native prebuild validation runners."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from audit_public import check_binary
from macos_build import PROJECT, workspace
from prepare_release import validate_host_format
from publish_release import api
from upload_release_asset import sha256


def validate(volume, platforms):
    state = json.loads((volume / "native-validation.json").read_text())
    release = api("/releases/" + str(state["release"]))
    if not release["prerelease"] or release["tag_name"] != "host-probes-23.1.6":
        raise ValueError("host validation requires the dedicated validation pre-release")
    staging = volume / "native-validation"
    staging.mkdir(exist_ok=True)
    assets = {asset["name"]: asset for asset in release["assets"]}
    manifest = json.loads((staging / "native-probes.json").read_text())

    def asset_for(path, digest):
        existing = assets.get(path.name)
        if existing is None:
            subprocess.run([sys.executable, "-B", str(PROJECT / "tools/upload_release_asset.py"),
                            str(release["id"]), str(path)], check=True)
            existing = next(asset for asset in api("/releases/" + str(release["id"]))["assets"]
                            if asset["name"] == path.name)
        if existing["digest"] != "sha256:" + digest or existing["size"] != path.stat().st_size:
            raise ValueError("remote validation asset does not match: " + path.name)
        return {"asset": existing["id"], "sha256": digest}

    for platform in platforms:
        suffix = ".exe" if platform == "windows" else ""
        binary = volume / "hosts" / (platform + "-x86_64") / "image" / ("LLVM" + suffix)
        validate_host_format(platform + "-x86_64", binary)
        problems = check_binary(binary)
        if problems:
            raise ValueError("\n".join(problems))
        digest = sha256(binary)
        name = staging / ("candidate-" + platform + "-" + digest[:16] + suffix)
        if not name.exists():
            os.link(binary, name)
        manifest[platform + "-llvm"] = asset_for(name, digest)

    encoded = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(encoded).hexdigest()
    path = staging / ("native-candidates-" + digest[:16] + ".json")
    path.write_bytes(encoded)
    record = asset_for(path, digest)
    body = {"ref": "main", "inputs": {"manifest_asset": str(record["asset"]),
                                      "manifest_sha256": record["sha256"]}}
    subprocess.run(["ghx", "api", "repos/Hexadecimall/LLVM-CLI/actions/workflows/native-probes.yml/dispatches",
                    "--method", "POST", "--body", json.dumps(body)], cwd=PROJECT, check=True)
    print("Dispatched native execution for " + ", ".join(platforms))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", nargs="+", choices=("linux", "windows"))
    validate(workspace(), args.platform)
