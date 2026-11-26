"""Rename a tool entry point in native objects or ThinLTO bitcode."""

from pathlib import Path
import re
import subprocess


def rename_entry(source: Path, target: Path, name: str,
                 bootstrap: Path, objcopy: Path, compiler=None) -> None:
    with source.open("rb") as stream:
        magic = stream.read(4)
    if magic not in (b"BC\xc0\xde", b"\xde\xc0\x17\x0b"):
        subprocess.run([str(objcopy), "--redefine-sym", f"_main=_{name}",
                        str(source), str(target)], check=True)
        return
    ir = target.with_suffix(target.suffix + ".ll")
    if compiler:
        # Keep Xcode's bitcode summary version and Darwin-specific attributes.
        subprocess.run([str(compiler), "-x", "ir", "-S", "-emit-llvm", "-O3",
                        "-flto=thin", "-target", "arm64-apple-macosx27.0.0",
                        str(source), "-o", str(ir)], check=True)
    else:
        subprocess.run([str(bootstrap), "dis", str(source), "-o", str(ir)], check=True)
    text = ir.read_text()
    if not re.search(r"^define\b[^\n]*@main\(", text, re.MULTILINE):
        raise RuntimeError(f"no main definition in {source.name}")
    text = re.sub(r"@main(?![-a-zA-Z$._0-9])", "@" + name, text)
    # Recompute ThinLTO summaries after renaming. Xcode can print private summary
    # records its textual parser does not accept, even for its own bitcode.
    text = re.sub(r"^\^.*\n?", "", text, flags=re.MULTILINE)
    ir.write_text(text)
    if compiler:
        subprocess.run([str(compiler), "-x", "ir", "-c", "-emit-llvm", "-O3",
                        "-flto=thin", "-target", "arm64-apple-macosx27.0.0",
                        str(ir), "-o", str(target)], check=True)
    else:
        subprocess.run([str(bootstrap), "as", str(ir), "-o", str(target)], check=True)
    ir.unlink()
