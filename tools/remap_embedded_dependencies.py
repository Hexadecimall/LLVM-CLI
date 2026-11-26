#!/usr/bin/env python3
"""Repair generated Make dependencies from bootstrap compilers with virtual headers.

Embedded resource contents belong to the compiler executable. Tracking that
physical file preserves invalidation without exposing or copying its headers.
"""

import re
from pathlib import Path


VIRTUAL = re.compile(r"/__llvm/(?:\\[^\r\n]|[^\s\\:])+")


def remap(root, compiler):
    compiler = str(Path(compiler).absolute())
    replacement = compiler.replace("\\", "\\\\").replace("$", "$$")
    for character in (" ", "#", ":"):
        replacement = replacement.replace(character, "\\" + character)
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or \
                (path.suffix != ".d" and path.name not in
                 {"compiler_depend.make", "compiler_depend.internal"}):
            continue
        before = path.read_text()
        value = compiler if path.name == "compiler_depend.internal" else replacement
        after = VIRTUAL.sub(lambda match: value, before)
        if before != after:
            path.write_text(after)
            changed += 1
    return changed
