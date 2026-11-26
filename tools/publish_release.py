#!/usr/bin/env python3
"""Publish audited native prebuilds using one reusable release-part file."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from audit_public import check_binary, check_source
from prepare_release import artifact, split_binary, split_gzip_binary, validate_host_format


PROJECT = Path(__file__).resolve().parent.parent
REPOSITORY = "repos/Hexadecimall/LLVM-CLI"


def api(path, method="GET", body=None):
    command = ["ghx", "api", REPOSITORY + path, "--method", method]
    if body is not None:
        command += ["--body", json.dumps(body)]
    result = subprocess.run(command, cwd=PROJECT, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True, help="pushed commit SHA")
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--binary", action="append", type=artifact, required=True)
    parser.add_argument("--publish", action="store_true", help="publish after verifying every uploaded asset")
    parser.add_argument("--gzip", action="store_true", help="stream compressed 256 MiB parts")
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        parser.error("expected a stable release tag such as v1.8.0")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be a complete pushed commit SHA")
    if len({host for host, _ in args.binary}) != len(args.binary):
        parser.error("duplicate host binary")
    problems = check_source()
    for host, binary in args.binary:
        validate_host_format(host, binary)
        problems += check_binary(binary)
    if problems:
        raise ValueError("\n".join(problems))
    # Never replace or append assets to an already public release.
    releases = api("/releases?per_page=100")
    existing = [release for release in releases if release["tag_name"] == args.tag]
    if existing:
        release = existing[0]
        if not release["draft"] or release["target_commitish"] != args.commit:
            raise ValueError("tag exists publicly or targets another commit")
    else:
        release = api("/releases", "POST", {
            "tag_name": args.tag, "target_commitish": args.commit,
            "name": "LLVM-CLI " + args.tag, "body": args.notes.read_text(), "draft": True,
        })
    release_id = release["id"]
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    uploaded = {}
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def upload(path):
        import hashlib
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(8 * 1024**2), b""):
                digest.update(block)
        expected_digest = "sha256:" + digest.hexdigest()
        uploaded[path.name] = (path.stat().st_size, expected_digest)
        remote = assets.get(path.name)
        if remote:
            if remote.get("size") != path.stat().st_size or remote.get("digest") != expected_digest:
                raise ValueError("existing draft asset differs: " + path.name)
            print("Reused verified draft asset: " + path.name, flush=True)
        else:
            subprocess.run([sys.executable, "-B", str(PROJECT / "tools/upload_release_asset.py"),
                            str(release_id), str(path)], check=True)
        # Only files exclusively created by this invocation reach this callback.
        path.unlink()

    manifest = ["llvm-cli-release-v2" if args.gzip else "llvm-cli-release-v1"]
    splitter = split_gzip_binary if args.gzip else split_binary
    for host, binary in args.binary:
        manifest += splitter(host, binary, args.output_dir, on_part=upload)
    path = args.output_dir / "llvm-cli-manifest-v1.txt"
    with path.open("x", encoding="ascii") as output:
        output.write("\n".join(manifest) + "\n")
    manifest_text = path.read_text()
    upload(path)
    verified = api(f"/releases/{release_id}")
    expected_names = {"llvm-cli-manifest-v1.txt"}
    for row in manifest[1:]:
        fields = row.split()
        if fields[0] == "part":
            expected_names.add(f"llvm-cli-{fields[1]}.part{int(fields[2]):03d}")
    if {a["name"] for a in verified["assets"]} != expected_names:
        raise ValueError("draft asset inventory differs from manifest; leaving draft unpublished")
    for asset in verified["assets"]:
        if (asset["size"], asset.get("digest")) != uploaded[asset["name"]]:
            raise ValueError("uploaded asset size or SHA-256 differs; leaving draft unpublished")
    (args.output_dir / "verified-manifest.txt").write_text(manifest_text)
    if args.publish:
        api(f"/releases/{release_id}", "PATCH", {"draft": False, "make_latest": "true"})
    print(f"{'Published' if args.publish else 'Prepared draft'}: https://github.com/Hexadecimall/LLVM-CLI/releases/tag/{args.tag}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"release publication: {error}", file=sys.stderr)
        raise SystemExit(1)
