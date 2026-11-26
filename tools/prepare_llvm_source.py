#!/usr/bin/env python3
"""Prepare the pinned LLVM source, reusing APFS blocks and local embedding patches."""

import argparse
import difflib
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import subprocess
import tarfile


PROJECT = Path(__file__).resolve().parent.parent
RELEASE = json.loads((PROJECT / "config/llvm-release.json").read_text())


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def relative_name(member):
    parts = PurePosixPath(member.name).parts
    if not parts or any(part in ("..", "") for part in parts):
        raise ValueError("unsafe archive member")
    return Path(*parts[1:]) if len(parts) > 1 else Path(".")


def local_changes(archive, source, exclude=()):
    patches = []
    with tarfile.open(archive, "r|xz") as upstream:
        for member in upstream:
            if not member.isfile():
                continue
            relative = relative_name(member)
            if relative.as_posix() in exclude:
                continue
            current = source / relative
            if not current.is_file() or current.is_symlink():
                continue
            original = upstream.extractfile(member).read()
            local = current.read_bytes()
            if original == local:
                continue
            try:
                before = original.decode("utf-8").splitlines(keepends=True)
                after = local.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                raise RuntimeError(f"local binary modification needs review: {relative}")
            if (original and not original.endswith(b"\n")) or (local and not local.endswith(b"\n")):
                raise RuntimeError(f"unterminated modified source needs review: {relative}")
            patches.extend(difflib.unified_diff(before, after,
                fromfile=f"a/{relative}", tofile=f"b/{relative}"))
            print(f"Preserving local patch: {relative}", flush=True)
    return "".join(patches)


def prepare(workspace, resume_reviewed=False):
    version = RELEASE["version"]
    baseline = PROJECT / "recovered/piper-llvm"
    source = baseline / f'llvm-project-{RELEASE["baseline_version"]}.src'
    old_archive = baseline / f'llvm-project-{RELEASE["baseline_version"]}.src.tar.xz'
    candidate = workspace / "source"
    ready = workspace / "source-ready.json"
    if ready.exists():
        if json.loads(ready.read_text())["sha256"] != RELEASE["source_sha256"]:
            raise RuntimeError("workspace source belongs to a different release")
        print(f"Reusing prepared LLVM {version} source", flush=True)
        return
    if candidate.exists():
        if not resume_reviewed:
            raise RuntimeError("unfinished source preparation exists; inspect it before retrying")
        versions = (candidate / "cmake/Modules/LLVMVersion.cmake").read_text()
        for name, value in zip(("MAJOR", "MINOR", "PATCH"), version.split(".")):
            if not re.search(r"set\(LLVM_VERSION_" + name + r" " + value + r"\)", versions):
                raise RuntimeError("reviewed source does not match the pinned version")
        if list(candidate.rglob("*.rej")):
            raise RuntimeError("unresolved patch rejects remain")
        ready.write_text(json.dumps({"version": version, "sha256": RELEASE["source_sha256"],
                                    "reviewed_resume": True}, indent=2) + "\n")
        return
    have_baseline = source.is_dir() and old_archive.is_file()
    downloads = workspace / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"llvm-project-{version}.src.tar.xz"
    if not archive.exists():
        subprocess.run(["curl", "--fail", "--location", "--retry", "3",
                        "--proto", "=https", "--proto-redir", "=https",
                        "--output", str(archive), RELEASE["source_url"]], check=True)
    if digest(archive) != RELEASE["source_sha256"]:
        raise RuntimeError("LLVM source SHA-256 mismatch; archive was kept for inspection")
    patch = (local_changes(old_archive, source,
                           exclude={"llvm/cmake/modules/FindLibXml2.cmake",
                                    "llvm/tools/llvm-config/llvm-config.cpp",
                                    "llvm/cmake/modules/FindLibEdit.cmake",
                                    "lldb/cmake/modules/FindPythonAndSwig.cmake",
                                    "lldb/bindings/lua/CMakeLists.txt",
                                    "lldb/bindings/python/CMakeLists.txt"})
             if have_baseline else
             (PROJECT / "patches/llvm-cli-embedding.patch").read_text())
    patchfile = workspace / "preserved-source.patch"
    patchfile.write_text(patch)
    # Clone on the same filesystem; a size-capped disk image needs a normal copy.
    if have_baseline:
        mode = "-cR" if source.stat().st_dev == workspace.stat().st_dev else "-pR"
        subprocess.run(["cp", mode, str(source), str(candidate)], check=True)
    else:
        candidate.mkdir()
    written = 0
    reused = 0
    new_paths = set()
    with tarfile.open(archive, "r|xz") as upstream:
        for member in upstream:
            relative = relative_name(member)
            new_paths.add(relative)
            target = candidate / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                data = upstream.extractfile(member).read()
                if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
                    reused += 1
                    continue
                if target.is_symlink():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(member.mode & 0o777)
                written += 1
            elif member.issym():
                if PurePosixPath(member.linkname).is_absolute():
                    raise RuntimeError(f"absolute source symlink: {relative}")
                if target.is_symlink() and target.readlink() == Path(member.linkname):
                    continue
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                raise RuntimeError(f"unsupported source archive member: {relative}")
    # Remove only files retired upstream, inside this newly cloned candidate.
    if have_baseline:
        with tarfile.open(old_archive, "r|xz") as old:
            for member in old:
                relative = relative_name(member)
                if (member.isfile() or member.issym()) and relative not in new_paths:
                    target = candidate / relative
                    if target.exists() or target.is_symlink():
                        target.unlink()
    if patch:
        subprocess.run(["patch", "--batch", "--forward", "-p1", "-i", str(patchfile)],
                       cwd=candidate, check=True)
    for name in ("llvm-23.1.6-static-libxml.patch", "llvm-config-embedded-prefix.patch",
                 "llvm-static-libedit.patch", "llvm-swig-library-path.patch",
                 "llvm-static-python.patch", "clang-embedded-dependencies.patch"):
        subprocess.run(["patch", "--batch", "--forward", "-p1", "-i",
                        str(PROJECT / "patches" / name)], cwd=candidate, check=True)
    ready.write_text(json.dumps({"version": version, "sha256": RELEASE["source_sha256"],
                                "reused_files": reused, "updated_files": written}, indent=2) + "\n")
    print(f"LLVM {version}: reused {reused} files; updated {written}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--resume-reviewed", action="store_true",
                        help="mark a manually reviewed, conflict-free source update ready")
    args = parser.parse_args()
    prepare(args.workspace.resolve(), args.resume_reviewed)


if __name__ == "__main__":
    main()
