#!/usr/bin/env python3
"""Freeze idle generated build inputs with verified, reversible Zstandard storage.

Run with `llvm python tools/build_cache.py freeze|thaw BUILD_DIRECTORY`.
Source files and native host executables remain in place. Every retired file
has a durable frame and journal entry before its original is unlinked.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import uuid


PROJECT = Path(__file__).resolve().parent.parent
SUFFIXES = {".o", ".a", ".pch", ".make", ".d", ".internal", ".inc"}


def cache_directory(root):
    return root.parent / (root.name + "-frozen")


def sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def make_directory(path):
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir()
        sync_directory(directory.parent)


def checked_root(value):
    root = Path(value).resolve(strict=True)
    if not root.is_relative_to(PROJECT / "build-work") or root.name != "build" or \
            not (root / "CMakeCache.txt").is_file():
        raise ValueError("only generated build-work/.../build directories with a CMake cache are supported")
    return root


def relative_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\0" in value:
        raise ValueError("unsafe cache journal path")
    return path


def records(cache):
    journal = cache / "journal.jsonl"
    result = {}
    if not journal.exists():
        return result
    data = journal.read_bytes()
    for line in data.splitlines(keepends=True):
        # A partial final append precedes retiring its raw file and is recoverable.
        if not line.endswith(b"\n"):
            break
        entry = json.loads(line)
        relative_path(entry["path"])
        result[entry["path"]] = entry
    return result


def append_record(journal, entry):
    journal.write(json.dumps(entry, separators=(",", ":")) + "\n")
    journal.flush()
    os.fsync(journal.fileno())


def prepare_journal(cache):
    path = cache / "journal.jsonl"
    if path.exists():
        with path.open("r+b") as journal:
            data = journal.read()
            if data and not data.endswith(b"\n"):
                journal.truncate(data.rfind(b"\n") + 1)
                journal.flush()
                os.fsync(journal.fileno())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def freeze(root):
    from compression import zstd
    cache = cache_directory(root)
    make_directory(cache)
    header = cache / "metadata.json"
    metadata = {"format": 1, "root": root.relative_to(PROJECT).as_posix()}
    if header.exists():
        if json.loads(header.read_text()) != metadata:
            raise ValueError("cache metadata belongs to another build")
    else:
        with header.open("x") as output:
            json.dump(metadata, output)
            output.flush()
            os.fsync(output.fileno())
    count = saved = linked = 0
    prepare_journal(cache)
    with (cache / "journal.jsonl").open("a", encoding="utf-8") as journal:
        sync_directory(cache)
        for path in sorted(root.rglob("*")):
            if path.suffix not in SUFFIXES or path.is_symlink() or not path.is_file():
                continue
            before = path.stat()
            if before.st_nlink != 1:
                linked += 1
                continue
            name = path.relative_to(root).as_posix()
            data = path.read_bytes()
            expected = digest(data)
            frame = cache / "files" / (name + ".zst")
            make_directory(frame.parent)
            reusable = frame.is_file() and digest(zstd.decompress(frame.read_bytes())) == expected
            if not reusable:
                compressed = zstd.compress(data, level=9)
                if len(compressed) >= before.st_size or \
                        shutil.disk_usage(cache).free < len(compressed) + 64 * 1024**2:
                    continue
                temporary = frame.with_name(frame.name + "." + uuid.uuid4().hex)
                try:
                    with temporary.open("xb") as output:
                        output.write(compressed)
                        output.flush()
                        os.fsync(output.fileno())
                    if digest(zstd.decompress(temporary.read_bytes())) != expected:
                        raise ValueError("compressed cache verification failed: " + name)
                    os.replace(temporary, frame)
                    sync_directory(frame.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            after = path.stat()
            if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink) != \
                    (before.st_ino, before.st_size, before.st_mtime_ns, 1):
                raise ValueError("build input changed during compression; leave the build idle")
            entry = {"path": name, "sha256": expected, "size": len(data),
                     "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns,
                     "atime_ns": before.st_atime_ns, "state": "frozen"}
            append_record(journal, entry)
            path.unlink()
            count += 1
            saved += before.st_size - frame.stat().st_size
            if count % 500 == 0:
                print(f"Frozen {count} inputs; saved {saved / 1024**3:.2f} GiB", flush=True)
    print(f"Frozen {count} inputs; saved {saved / 1024**3:.2f} GiB; kept {linked} shared inputs")


def thaw(root):
    from compression import zstd
    cache = cache_directory(root)
    if not cache.exists():
        return
    if json.loads((cache / "metadata.json").read_text()) != \
            {"format": 1, "root": root.relative_to(PROJECT).as_posix()}:
        raise ValueError("cache metadata belongs to another build")
    count = 0
    entries = records(cache)
    if not entries:
        return
    prepare_journal(cache)
    with (cache / "journal.jsonl").open("a", encoding="utf-8") as journal:
        for name, entry in entries.items():
            if entry["state"] != "frozen":
                continue
            target = root.joinpath(*relative_path(name).parts)
            if not target.resolve().is_relative_to(root) or target.is_symlink():
                raise ValueError("unsafe restore destination: " + name)
            frame = cache / "files" / (name + ".zst")
            if target.exists():
                if digest(target.read_bytes()) != entry["sha256"]:
                    raise ValueError("refusing to replace a changed generated file: " + name)
            else:
                data = zstd.decompress(frame.read_bytes())
                if len(data) != entry["size"] or digest(data) != entry["sha256"]:
                    raise ValueError("cache frame checksum mismatch: " + name)
                if shutil.disk_usage(root).free < len(data) + 64 * 1024**2:
                    raise ValueError("not enough space to restore inputs; freeze the other idle host build first")
                make_directory(target.parent)
                temporary = target.with_name(target.name + ".restore-" + uuid.uuid4().hex)
                try:
                    with temporary.open("xb") as output:
                        output.write(data)
                        output.flush()
                        if digest(temporary.read_bytes()) != entry["sha256"]:
                            raise ValueError("restored input checksum mismatch: " + name)
                        temporary.chmod(entry["mode"])
                        os.utime(temporary, ns=(entry["atime_ns"], entry["mtime_ns"]))
                        os.fsync(output.fileno())
                    os.replace(temporary, target)
                    sync_directory(target.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            append_record(journal, dict(entry, state="active"))
            frame.unlink(missing_ok=True)
            count += 1
    print(f"Restored {count} verified inputs with their original modes and timestamps")


def restore_build_cache(root):
    """Let the embedded interpreter provide the codec for older build Python."""
    root = Path(root).resolve()
    if not (cache_directory(root) / "journal.jsonl").is_file():
        return
    if not any(entry["state"] == "frozen" for entry in records(cache_directory(root)).values()):
        return
    bootstrap = os.environ.get("LLVM_CLI_BOOTSTRAP") or shutil.which("llvm")
    if not bootstrap:
        raise RuntimeError("set LLVM_CLI_BOOTSTRAP to restore the frozen build inputs")
    subprocess.run([bootstrap, "python", str(Path(__file__).resolve()), "thaw", str(root)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("freeze", "thaw", "status"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        root = checked_root(args.root)
        if args.operation == "freeze":
            freeze(root)
        elif args.operation == "thaw":
            thaw(root)
        else:
            frozen = [e for e in records(cache_directory(root)).values() if e["state"] == "frozen"]
            print(f"Frozen: {len(frozen)} files, {sum(e['size'] for e in frozen) / 1024**3:.2f} GiB before compression")
    except (OSError, ValueError, ImportError) as error:
        print(f"build cache: {error}", file=sys.stderr)
        raise SystemExit(1)
