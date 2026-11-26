#!/usr/bin/env python3
"""Stage updated SDK resources without copying the retained payload tree."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from build_native_bundles import REUSABLE, active_link_files, recipes as tool_recipes
from macos_build import PROJECT, RELEASE, workspace, build_environment, static_python_libraries
from materialize_archive import materialize


def static_recipes(build):
    build = Path(build).resolve()
    result = {}
    for recipe in active_link_files(build):
        tokens = shlex.split(recipe.read_text())
        if "-o" in tokens:
            candidates = [tokens[tokens.index("-o") + 1]]
        else:
            candidates = tokens
        for token in candidates:
            if token.endswith(".a"):
                output = (recipe.parent.parent.parent / token).resolve()
                if output.parent == build / "lib":
                    result.setdefault(output.name, (output, recipe.parent.stem))
                break
    return result


def link_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    source = source.resolve(strict=True)
    if target.is_file() and os.path.samefile(source, target):
        return
    temporary = target.with_name(target.name + ".next")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.link(source, temporary)
    os.replace(temporary, target)


def link_headers(source, target, suffixes=None):
    if not source.is_dir():
        return
    for path in source.rglob("*"):
        if path.is_file() and (suffixes is None or path.suffix in suffixes):
            link_file(path, target / path.relative_to(source))


def fortran_runtime(volume, jobs):
    """Build only Flang's runtime with the newly built compiler tools."""
    build = volume / "build"
    runtime = volume / "fortran-runtimes"
    environment = build_environment(volume)
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    flags = "-ffile-prefix-map=" + str(volume) + "=/__llvm-build"
    configure = ["cmake", "-S", str(volume / "source/runtimes"), "-B", str(runtime),
                 "-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release",
                 "-DLLVM_ENABLE_RUNTIMES=flang-rt", "-DLLVM_ENABLE_LTO=OFF",
                 "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
                 "-DLLVM_INCLUDE_TESTS=OFF", "-DFLANG_RT_INCLUDE_TESTS=OFF",
                 "-DFLANG_RT_ENABLE_STATIC=ON", "-DFLANG_RT_ENABLE_SHARED=OFF",
                 "-DFLANG_RT_FORTRAN_MODULES=ON", "-DFORTRAN_SUPPORTS_REAL16=OFF",
                 "-DCMAKE_Fortran_COMPILER_WORKS=ON",
                 "-DCMAKE_OSX_DEPLOYMENT_TARGET=27.0", "-DCMAKE_OSX_SYSROOT=" + sdk,
                 "-DCMAKE_INSTALL_PREFIX=/__llvm", "-DLLVM_BINARY_DIR=" + str(build),
                 "-DCMAKE_C_FLAGS=" + flags, "-DCMAKE_CXX_FLAGS=" + flags]
    for language, name in (("C", "clang"), ("CXX", "clang++"), ("Fortran", "flang")):
        configure.append("-DCMAKE_" + language + "_COMPILER=" + str(build / "bin" / name))
    with (volume / "logs/fortran-runtime-build.log").open("a") as log:
        for command in (configure, ["cmake", "--build", str(runtime), "--parallel", str(jobs)]):
            subprocess.run(command, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True)


def libraries(volume, available, jobs):
    build = volume / "build"
    recipes = static_recipes(build)
    environment = build_environment(volume)
    missing = sorted(target for source, target in recipes.values() if not source.is_file())
    shared = []
    for recipe in active_link_files(build):
        tokens = shlex.split(recipe.read_text())
        if "-o" in tokens:
            output = (recipe.parent.parent.parent / tokens[tokens.index("-o") + 1]).resolve()
            if output.parent == build / "lib" and output.suffix in (".dylib", ".so"):
                shared.append(recipe.parent.stem)
    if not available:
        with (volume / "logs/sdk-libraries-build.log").open("a") as log:
            subprocess.run(["cmake", "--build", str(build), "--target", *missing, *shared,
                            "bolt_rt",
                            "--parallel", str(jobs)], env=environment,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        fortran_runtime(volume, jobs)
    # External runtime projects copy their archives here without a parent-tree
    # link recipe (for example BOLT's instrumentation runtimes).
    for source in (build / "lib").glob("*.a"):
        recipes.setdefault(source.name, (source, None))
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    for index, (name, (source, _)) in enumerate(sorted(recipes.items()), 1):
        if not source.is_file():
            continue
        print(f"[{index}/{len(recipes)}] SDK archive {name}", flush=True)
        materialize(source, volume / "public-libraries" / name, compiler, jobs)
    return recipes


def stage(volume):
    baseline = PROJECT / "recovered/llvm-all/inmemory/payload"
    manifest = json.loads((volume / "bundle-manifest.json").read_text())
    required = {p.stem for p in (baseline / "bundles").glob("*.bundle")} - REUSABLE
    required |= tool_recipes(volume / "build").keys() - {"llvm"}
    if manifest["version"] != RELEASE["version"] or manifest["failures"] or \
            required - set(manifest["completed"]):
        raise RuntimeError("finish and verify all LLVM modules before staging the release payload")
    baseline_config = baseline / "root/include/lldb/Host/Config.h"
    candidate_config = volume / "build/tools/lldb/include/lldb/Host/Config.h"
    required_features = set(re.findall(r"^#define (LLDB_ENABLE_\w+) 1$",
                                      baseline_config.read_text(), re.MULTILINE))
    enabled_features = set(re.findall(r"^#define (LLDB_ENABLE_\w+) 1$",
                                     candidate_config.read_text(), re.MULTILINE))
    if required_features - enabled_features:
        raise RuntimeError("baseline LLDB features were disabled: " +
                           ", ".join(sorted(required_features - enabled_features)))
    overlay = volume / "overlay"
    root = overlay / "root"
    borrowed = {
        "zstd": ["lib/libzstd.a", "include/zstd.h", "include/zstd_errors.h"],
        "ncurses": ["lib/libncursesw.a", "lib/libpanelw.a"],
        "xz": ["lib/liblzma.a"],
        "tree-sitter": ["lib/libtree-sitter.a"],
        "openssl@3": ["lib/libssl.a", "lib/libcrypto.a"],
        "sqlite": ["lib/libsqlite3.a", "include/sqlite3.h", "include/sqlite3ext.h"],
        "mpdecimal": ["lib/libmpdec.a", "include/mpdecimal.h"],
    }
    for package, files in borrowed.items():
        for name in files:
            source = Path("/opt/homebrew/opt") / package / name
            target = root / "deps" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                shutil.copyfile(source, target)
    for package in ("ncurses", "xz", "tree-sitter", "openssl@3"):
        shutil.copytree(Path("/opt/homebrew/opt") / package / "include",
                        root / "deps/include", dirs_exist_ok=True)
    for library in static_python_libraries():
        if "build-python-static" in library.parts:
            name = "libexpat-python.a" if library.name == "libexpat.a" else library.name
            target = root / "deps/lib" / name
            if not target.is_file() or target.read_bytes() != library.read_bytes():
                shutil.copyfile(library, target)
    for name in ("ncurses", "panel"):
        link_file(root / ("deps/lib/lib" + name + "w.a"),
                  root / ("deps/lib/lib" + name + ".a"))
    for pc in (baseline / "root/deps/lib/pkgconfig").glob("*.pc"):
        target = root / "deps/lib/pkgconfig" / pc.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(re.sub(r"^prefix=.*$", "prefix=${pcfiledir}/../..",
                                 pc.read_text(), flags=re.MULTILINE))
    zstd_config = root / "deps/lib/cmake/zstd/zstdConfig.cmake"
    zstd_config.parent.mkdir(parents=True, exist_ok=True)
    zstd_config.write_text(
        'get_filename_component(_zstd_prefix "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)\n'
        'if(NOT TARGET zstd::libzstd_static)\n'
        '  add_library(zstd::libzstd_static STATIC IMPORTED)\n'
        '  set_target_properties(zstd::libzstd_static PROPERTIES\n'
        '    IMPORTED_LOCATION "${_zstd_prefix}/lib/libzstd.a"\n'
        '    INTERFACE_INCLUDE_DIRECTORIES "${_zstd_prefix}/include")\n'
        'endif()\nunset(_zstd_prefix)\n')
    headers = {".h", ".inc", ".td", ".def", ".modulemap", ".imp", ".pdll"}
    projects = ["llvm", "clang", "clang-tools-extra", "lld", "lldb", "flang", "mlir", "polly"]
    for project in projects:
        link_headers(volume / "source" / project / "include", root / "include", headers)
        generated = volume / "build" / ("include" if project == "llvm" else
                                         "tools/" + project + "/include")
        link_headers(generated, root / "include", headers)
    link_headers(volume / "public-runtimes/include/c++/v1", root / "include/c++/v1")
    link_headers(volume / "build/lib/clang/23/include", root / "lib/clang/23/include")
    for directory in [volume / "public-libraries", volume / "public-runtimes/lib"]:
        for archive in directory.glob("*.a"):
            link_file(archive, root / "lib" / archive.name)
    for runtime in (volume / "compiler-rt/lib/darwin").glob("libclang_rt*"):
        link_file(runtime, root / "lib/clang/23/lib/darwin" / runtime.name)
    link_headers(volume / "build/lib/clang/23/finclude", root / "lib/clang/23/finclude")
    link_headers(volume / "build/runtimes/runtimes-bins/lib/clang/23/finclude",
                 root / "lib/clang/23/finclude")
    link_headers(volume / "fortran-runtimes/lib/clang/23/finclude",
                 root / "lib/clang/23/finclude")
    for runtime in (volume / "build/lib/clang/23/lib/darwin").glob("*.a"):
        link_file(runtime, root / "lib/clang/23/lib/darwin" / runtime.name)
    for runtime in (volume / "build/runtimes/runtimes-bins/lib/clang/23/lib/darwin").glob("*.a"):
        link_file(runtime, root / "lib/clang/23/lib/darwin" / runtime.name)
    for runtime in (volume / "fortran-runtimes/lib/clang/23/lib/darwin").glob("*.a"):
        link_file(runtime, root / "lib/clang/23/lib/darwin" / runtime.name)
    shared_libraries = (library for directory in (volume / "build/lib", volume / "public-runtimes/lib")
                        for library in directory.iterdir())
    for library in shared_libraries:
        if library.suffix not in (".so", ".dylib") or not library.exists():
            continue
        destination = root / "lib" / library.name
        if library.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            destination.symlink_to(Path(os.readlink(library)).name)
        else:
            link_file(library, destination)
    link_headers(volume / "build/lib/python3.14/site-packages/lldb",
                 root / "lib/python3.14/site-packages/lldb", {".py"})
    exclusions = []
    for path in (baseline / "root/lib/cmake").rglob("*"):
        if path.is_file() or path.is_symlink():
            exclusions.append(path.relative_to(baseline).as_posix())
    new_lldb = root / ("lib/liblldb." + RELEASE["version"] + ".dylib")
    if new_lldb.is_file():
        exclusions.extend(path.relative_to(baseline).as_posix()
                          for path in (baseline / "root/lib").glob("liblldb.*.dylib")
                          if not path.is_symlink() and path.name != new_lldb.name)
    # The install component emits relocatable config files, unlike build-tree exports.
    environment = build_environment(volume)
    for component in ["cmake-exports", "clang-cmake-exports", "lld-cmake-exports",
                      "mlir-cmake-exports", "flang-cmake-exports"]:
        with (volume / "logs/sdk-config-stage.log").open("a") as log:
            subprocess.run(["cmake", "--install", str(volume / "build"), "--prefix", str(root),
                            "--component", component], env=environment,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    # Polly installs these files under the unscoped component; stage just its
    # relocatable configs instead of installing unrelated standalone outputs.
    polly_configs = volume / "build/tools/polly/cmake/CMakeFiles"
    for name in ("PollyConfig.cmake", "PollyExports-all.cmake"):
        link_file(polly_configs / name, root / "lib/cmake/polly" / name)
    config = root / "lib/cmake/llvm/LLVMConfig.cmake"
    if not config.is_file() or ("set(LLVM_PACKAGE_VERSION " + RELEASE["version"] + ")") \
            not in config.read_text():
        raise RuntimeError("updated relocatable LLVM CMake config was not installed")
    config_text = config.read_text()
    config_text = config_text.replace('set(ZLIB_ROOT )',
                                      'set(ZLIB_ROOT "${LLVM_INSTALL_PREFIX}/deps")')
    config_text = config_text.replace('list(APPEND CMAKE_MODULE_PATH',
        'list(PREPEND CMAKE_PREFIX_PATH "${LLVM_INSTALL_PREFIX}/deps")\n'
        'set(LIBXML2_EXTRA_STATIC_LIBRARIES\n'
        '  "${LLVM_INSTALL_PREFIX}/deps/lib/libz.a"\n'
        '  "${LLVM_INSTALL_PREFIX}/deps/lib/libiconv.a"\n'
        '  "${LLVM_INSTALL_PREFIX}/deps/lib/libcharset.a")\n'
        'list(APPEND CMAKE_MODULE_PATH', 1)
    config_text = re.sub(r'set\(LLVM_DEFAULT_EXTERNAL_LIT "[^"]*"\)',
                        'set(LLVM_DEFAULT_EXTERNAL_LIT "${LLVM_INSTALL_PREFIX}/bin/llvm-lit")',
                        config_text)
    config.write_text(config_text)
    notices = root / "share/licenses"
    shutil.copytree(PROJECT / "licenses", notices, dirs_exist_ok=True)
    for component, license_file in [("llvm-cli", PROJECT / "LICENSE"),
                                    ("llvm", volume / "source/llvm/LICENSE.TXT")]:
        destination = notices / component / "LICENSE"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_file, destination)
    (overlay / ".pack-exclude").write_text("".join(name + "\n" for name in sorted(exclusions)))
    record = {"llvm_tools": RELEASE["version"],
              "upstream_source_sha256": RELEASE["source_sha256"],
              "macos_compiler_rt": RELEASE["version"],
              "macos_cxx_static_runtime": RELEASE["version"],
              "macos_cxx_shared_runtime": RELEASE["version"],
              "macos_fortran_runtime": RELEASE["version"],
              "reused_dependency_modules": sorted(REUSABLE),
              "retained_resources": "Cross-target runtimes and independent resources from baseline payload"}
    notice = root / "share/llvm-cli/components.json"
    notice.parent.mkdir(parents=True, exist_ok=True)
    notice.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Payload overlay: {overlay}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--available", action="store_true",
                        help="materialize only existing SDK archives without starting native builds")
    parser.add_argument("--stage", action="store_true",
                        help="also stage headers, runtimes, and relocatable CMake resources")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    if args.available and args.stage:
        parser.error("--stage requires the complete SDK archive build")
    volume = workspace()
    from build_cache import restore_build_cache
    restore_build_cache(volume / "build")
    libraries(volume, args.available, args.jobs)
    if args.stage:
        stage(volume)


if __name__ == "__main__":
    main()
