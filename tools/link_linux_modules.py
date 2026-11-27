#!/usr/bin/env python3
"""Package Linux LLVM tools outside the multicall driver as embedded ELF modules."""

import argparse
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from build_native_bundles import active_link_files
from macos_build import workspace, build_environment
from rename_entry import rename_entry


def link_modules(volume, compiler, names):
    host = volume / "hosts/linux-x86_64"
    build = host / "build"
    work = host / "module-work"
    bundles = host / "image/modules/bundles"
    work.mkdir(exist_ok=True)
    bundles.mkdir(parents=True, exist_ok=True)
    runtime = volume / "runtime-work/linux-gnu"
    loader, = (runtime / "global/o").glob("*/libld.so.2")
    tools = {}
    for recipe in active_link_files(build):
        tokens = shlex.split(recipe.read_text())
        if tokens[:2] != [str(compiler), "c++"] or "-o" not in tokens:
            continue
        cwd = recipe.parent.parent.parent
        exe = (cwd / tokens[tokens.index("-o") + 1]).resolve()
        if exe.parent == build / "bin":
            tools[exe.name] = (cwd, tokens)

    environment = build_environment(volume)
    with (host / "modules.log").open("a") as logfile:
        for name in names:
            if not re.fullmatch(r"[a-z0-9_-]+", name) or name == "llvm" or name not in tools:
                raise ValueError("no standalone LLVM executable recipe for " + name)
            cwd, command = tools[name]
            inputs = []
            for token in command:
                if not token.endswith(".o") or "/CMakeFiles/" not in ("/" + token):
                    continue
                obj = (cwd / token).resolve()
                symbols = subprocess.check_output([str(compiler), "nm", "--defined-only", str(obj)],
                                                  text=True, env=environment)
                if re.search(r"\bT main$", symbols, re.M):
                    inputs.append((token, obj))
            if len(inputs) != 1:
                raise ValueError("expected one main entry object for " + name)
            main_token, main = inputs[0]
            symbol = "LLVM_" + name.replace("-", "_") + "_main"
            entry = work / (name + ".o")
            rename_entry(main, entry, symbol, compiler, compiler,
                         symbol_prefix="", objcopy_args=("objcopy",))
            exports = work / (name + ".exports")
            exports.write_text("{ global: " + symbol + "; local: *; };\n")
            module = work / (name + ".bundle")
            link = [str(entry) if arg == main_token else arg for arg in command
                    if not arg.endswith("/crt1.o") and arg not in {"-rdynamic", "-pie"}]
            link[link.index("-o") + 1] = str(module)
            link[2:2] = ["-shared", "-Wl,-s,-z,defs", "-Wl,--version-script=" + str(exports)]
            link.append(str(loader))
            subprocess.run(link, cwd=cwd, env=environment, stdout=logfile,
                           stderr=subprocess.STDOUT, check=True)
            output = bundles / (name + ".bundle.zst")
            pending = work / (name + ".next.zst")
            subprocess.run([str(volume / "tools/llvm-pack"), "--compress", str(module), str(pending)],
                           env=environment, stdout=logfile, stderr=subprocess.STDOUT, check=True)
            pending.replace(output)
            print("Embedded Linux module: " + name, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, default=Path(shutil.which("llvm") or "llvm"))
    parser.add_argument("tools", nargs="+")
    args = parser.parse_args()
    link_modules(workspace(), args.compiler.absolute(), args.tools)
