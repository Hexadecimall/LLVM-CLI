#!/usr/bin/env python3
"""Rebuild LLVM payload modules with one shared, bounded native build tree."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from macos_build import build_environment, workspace, PROJECT, RELEASE

# These are independent of LLVM's C++ ABI and retain their existing versions.
REUSABLE = frozenset("bzip2 bzip2recover ccache-swig curl iconv lua luac python3.14 "
                     "swig tree-sitter xmlcatalog xmllint z3".split())


def active_link_files(build):
    # Reconfiguration can leave obsolete link.txt files behind. Only current
    # targets from CMake's generated directory index are buildable.
    index = build / "CMakeFiles/TargetDirectories.txt"
    for directory in index.read_text().splitlines():
        path = (build / directory) / "link.txt"
        if path.is_file():
            yield path


def recipes(build):
    result = {}
    for path in active_link_files(build):
        tokens = shlex.split(path.read_text())
        if "-o" not in tokens:
            continue
        output = (path.parent.parent.parent / tokens[tokens.index("-o") + 1]).resolve()
        if output.parent == build / "bin":
            result[output.name] = path
    return result


def link_inputs(recipe):
    root = recipe.parent.parent.parent
    return [(root / token).resolve() for token in shlex.split(recipe.read_text())
            if token.endswith((".o", ".a", ".dylib", ".tbd")) and not token.startswith("-")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--relink-only", action="store_true",
                        help="reuse existing native inputs; compile a target only if an input is missing")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    volume = workspace()
    build = volume / "build"
    environment = build_environment(volume)
    environment.update({"LLVM_CLI_NATIVE_BUILD_DIR": str(build),
                        "LLVM_CLI_STDLIB_DIR": str(volume / "runtimes/lib")})
    baseline = PROJECT / "recovered/llvm-all/inmemory/payload/bundles"
    required = sorted(path.stem for path in baseline.glob("*.bundle")
                      if path.stem not in REUSABLE)
    links = recipes(build)
    missing = set(required) - links.keys()
    if missing:
        raise RuntimeError("no native recipe for: " + ", ".join(sorted(missing)))
    required = sorted(set(required) | (links.keys() - {"llvm"}))
    if args.only:
        unknown = set(args.only) - set(required)
        if unknown:
            parser.error("unknown module: " + ", ".join(sorted(unknown)))
        required = args.only
    overlay = volume / "overlay/bundles"
    overlay.mkdir(parents=True, exist_ok=True)
    intermediates = volume / "bundle-work"
    intermediates.mkdir(exist_ok=True)
    packer = volume / "tools/llvm-pack"
    manifest_path = volume / "bundle-manifest.json"
    manifest = (json.loads(manifest_path.read_text()) if manifest_path.exists()
                else {"version": RELEASE["version"], "completed": [], "failures": {}})
    if manifest["version"] != RELEASE["version"]:
        raise RuntimeError("bundle manifest version does not match pinned release")
    source_time = max(path.stat().st_mtime_ns for path in (volume / "source").rglob("*")
                      if path.is_file())
    source_time = max(source_time, (build / "CMakeCache.txt").stat().st_mtime_ns)
    builder_inputs = [PROJECT / "tools" / name for name in
                      ("build_tool_bundle.py", "rename_entry.py", "lldb-python-entry.cpp")]
    builder_inputs += list((volume / "runtimes/lib").glob("*.a"))
    for index, name in enumerate(required, 1):
        compressed = overlay / (name + ".bundle.zst")
        link = links[name]
        inputs = link_inputs(link)
        newest_input = max(path.stat().st_mtime_ns for path in
                           [link, *inputs, *builder_inputs] if path.is_file())
        native_current = args.relink_only or source_time <= (
            compressed.stat().st_mtime_ns if compressed.is_file() else 0)
        if name in manifest["completed"] and compressed.is_file() and native_current and \
                newest_input <= compressed.stat().st_mtime_ns:
            print(f"[{index}/{len(required)}] {name}: reused completed module", flush=True)
            continue
        output = intermediates / (name + ".bundle")
        log = volume / "logs" / ("bundle-" + name + ".log")
        print(f"[{index}/{len(required)}] {name}: building", flush=True)
        try:
            with log.open("a") as stream:
                commands = []
                if not args.relink_only or any(not path.is_file() for path in inputs):
                    commands.append(["cmake", "--build", str(build), "--target", link.parent.stem,
                                     "--parallel", str(args.jobs)])
                commands += [
                    [sys.executable, "-B", str(PROJECT / "tools/build_tool_bundle.py"),
                     name, str(link), str(output)],
                    ["codesign", "-s", "-", "--force", str(output)],
                    [str(packer), "--compress", str(output), str(compressed) + ".next"],
                ]
                for command in commands:
                    subprocess.run(command, env=environment, stdout=stream,
                                   stderr=subprocess.STDOUT, check=True)
            os.replace(str(compressed) + ".next", compressed)
            output.unlink()
            if name not in manifest["completed"]:
                manifest["completed"].append(name)
            manifest["failures"].pop(name, None)
            print(f"[{index}/{len(required)}] {name}: compressed "
                  f"{compressed.stat().st_size / 1024 ** 2:.1f} MiB", flush=True)
        except subprocess.CalledProcessError as error:
            manifest["failures"][name] = {"exit": error.returncode,
                                           "log": str(log.relative_to(volume))}
            print(f"[{index}/{len(required)}] {name}: FAILED; see {log}", flush=True)
        staged_manifest = manifest_path.with_suffix(".json.next")
        staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(staged_manifest, manifest_path)
    return int(bool(manifest["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
