#!/usr/bin/env python3
"""Prepare and build pinned LLVM inside a durable, size-capped macOS workspace."""

import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

from prepare_llvm_source import prepare, PROJECT, RELEASE
from recovered_paths import relocate


def run(command, environment=None, logfile=None):
    print("Running: " + " ".join(map(str, command)), flush=True)
    if logfile:
        with logfile.open("a") as log:
            subprocess.run(list(map(str, command)), env=environment,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    else:
        subprocess.run(list(map(str, command)), env=environment, check=True)


def workspace():
    root = PROJECT / "build-work" / ("macos-" + RELEASE["version"])
    volume = root / "volume"
    image = root / "workspace.sparseimage"
    root.mkdir(parents=True, exist_ok=True)
    if not image.exists():
        if shutil.disk_usage(PROJECT).free < 40 * 1024 ** 3:
            raise RuntimeError("at least 40 GiB free is required before starting")
        capacity = RELEASE["workspace_limit_gib"] - 1
        if capacity <= 0 or capacity >= 25:
            raise RuntimeError("workspace capacity must remain below 25 GiB")
        run(["hdiutil", "create", "-size", str(capacity) + "g", "-type", "SPARSE",
             "-fs", "APFS", "-volname", "LLVMCLI236", "-nospotlight", image])
    volume.mkdir(exist_ok=True)
    if not volume.is_mount():
        run(["hdiutil", "attach", "-nobrowse", "-owners", "off",
             "-mountpoint", volume, image])
    return volume


def status():
    root = PROJECT / "build-work" / ("macos-" + RELEASE["version"])
    image = root / "workspace.sparseimage"
    volume = root / "volume"
    print(f"Pinned LLVM: {RELEASE['version']}")
    print(f"Additional-storage budget: {RELEASE['workspace_limit_gib']} GiB")
    if not image.is_file():
        print("Workspace has not been created")
        return
    print(f"Image allocated: {image.stat().st_blocks * 512 / 1024 ** 3:.2f} GiB")
    if not volume.is_mount():
        print("Workspace image is not mounted")
        return
    usage = shutil.disk_usage(volume)
    print(f"Workspace used: {usage.used / 1024 ** 3:.2f} GiB; "
          f"free: {usage.free / 1024 ** 3:.2f} GiB")
    ready = volume / "source-ready.json"
    print("Source: " + (json.loads(ready.read_text())["version"]
                        if ready.is_file() else "preparation incomplete"))
    manifest = volume / "bundle-manifest.json"
    if manifest.is_file():
        modules = json.loads(manifest.read_text())
        print(f"Completed tool modules: {len(modules['completed'])}")
        if modules["failures"]:
            print("Modules awaiting retry: " + ", ".join(sorted(modules["failures"])))
    logs = (volume / "logs").glob("*.log")
    log = max((path for path in logs if path.is_file()),
              key=lambda path: path.stat().st_mtime_ns, default=None)
    if log is not None:
        print("Latest build log: " + log.name)
        sys.stdout.flush()
        subprocess.run(["tail", "-n", "5", str(log)], check=True)
    print("Core artifacts are intermediate; full tool and runtime verification "
          "is required before installation.")


def build_environment(volume):
    environment = os.environ.copy()
    for name in ("tmp", "cache", "module-cache", "logs"):
        (volume / name).mkdir(exist_ok=True)
    environment.update({"TMPDIR": str(volume / "tmp") + "/",
                        "XDG_CACHE_HOME": str(volume / "cache"),
                        "CLANG_MODULE_CACHE_PATH": str(volume / "module-cache"),
                        "SWIG_LIB": str(PROJECT / "recovered/llvm-all/deps/share/swig/4.5.1"),
                        "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


def static_python_libraries():
    recovered = PROJECT / "recovered/llvm-all"
    libraries = [recovered / "deps/lib" / name for name in ("libbz2.a", "libffi.a")]
    libraries += sorted((recovered / "build-python-static/Modules/_hacl").glob("*.a"))
    libraries.append(recovered / "build-python-static/Modules/expat/libexpat.a")
    for package, name in (("openssl@3", "libssl.a"), ("openssl@3", "libcrypto.a"),
                          ("sqlite", "libsqlite3.a"), ("mpdecimal", "libmpdec.a")):
        libraries.append(Path("/opt/homebrew/opt") / package / "lib" / name)
    if len(libraries) < 13 or any(not path.is_file() for path in libraries):
        raise RuntimeError("the retained static Python dependency libraries are incomplete")
    return libraries


def configure(volume):
    environment = build_environment(volume)
    if not (volume / "required-tools.txt").exists():
        baseline = PROJECT / "recovered/llvm-all/inmemory/LLVM"
        tools = (subprocess.check_output([str(baseline), "list"], text=True)
                 if baseline.is_file() else
                 (PROJECT / "config/required-tools.txt").read_text())
        (volume / "required-tools.txt").write_text(tools)
    deps = PROJECT / "recovered/llvm-all/deps"
    pkgconfig = volume / "pkgconfig"
    pkgconfig.mkdir(exist_ok=True)
    for pc in (deps / "lib/pkgconfig").glob("*.pc"):
        (pkgconfig / pc.name).write_text(relocate(pc.read_text(), PROJECT))
    environment["PKG_CONFIG_PATH"] = str(pkgconfig)
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    cxx = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    flags = (f"-ffile-prefix-map={volume}=/__llvm-build "
             f"-ffile-prefix-map={PROJECT}=/__llvm-src")
    command = ["cmake", "-S", volume / "source/llvm", "-B", volume / "build",
               "-G", "Unix Makefiles", "-C", PROJECT / "config/macos-release.cmake",
               "-DCMAKE_C_COMPILER=" + compiler, "-DCMAKE_CXX_COMPILER=" + cxx,
               "-DCMAKE_OSX_SYSROOT=" + sdk, "-DCMAKE_OSX_DEPLOYMENT_TARGET=27.0",
               "-DCMAKE_C_FLAGS=" + flags, "-DCMAKE_CXX_FLAGS=" + flags,
               "-DCMAKE_PREFIX_PATH=" + str(deps),
               "-DCMAKE_INSTALL_PREFIX=" + str(volume / "install"),
               "-DPython3_EXECUTABLE=" + str(deps / "bin/python3.14"),
               "-DPython3_INCLUDE_DIR=" + str(deps / "include/python3.14"),
               "-DPython3_LIBRARY=" + str(deps / "lib/libpython3.14.a"),
               "-DLLDB_PYTHON_EXTRA_STATIC_LIBRARIES=" + ";".join(map(str, static_python_libraries())),
               "-DSWIG_EXECUTABLE=" + str(deps / "bin/swig"),
               "-DSWIG_DIR=" + str(deps / "share/swig/4.5.1"),
               "-DTreeSitter_LIBRARY=/opt/homebrew/opt/tree-sitter/lib/libtree-sitter.a",
               "-Dzstd_LIBRARY=/opt/homebrew/opt/zstd/lib/libzstd.a",
               "-U", "HAVE_LIBXML2",
               "-U", "__pkg_config_checked_PC_LIBXML",
               "-DZLIB_LIBRARY=" + str(deps / "lib/libz.a"),
               "-DZLIB_INCLUDE_DIR=" + str(deps / "include"),
               "-DLIBXML2_LIBRARY=" + str(deps / "lib/libxml2.a"),
               "-DLIBXML2_STATIC_LIBRARY=" + str(deps / "lib/libxml2.a"),
               "-DLIBXML2_INCLUDE_DIR=" + str(deps / "include/libxml2"),
               "-DLIBXML2_EXTRA_STATIC_LIBRARIES=" + ";".join(
                   str(deps / "lib" / name) for name in
                   ("libz.a", "libiconv.a", "libcharset.a")),
               "-DLibEdit_LIBRARIES=" + str(deps / "lib/libedit.a"),
               "-DLibEdit_TERMCAP_LIBRARY=/opt/homebrew/opt/ncurses/lib/libncursesw.a",
               "-DCURSES_INCLUDE_DIRS=/opt/homebrew/opt/ncurses/include",
               "-DCURSES_LIBRARIES=/opt/homebrew/opt/ncurses/lib/libncursesw.a",
               "-DPANEL_LIBRARIES=/opt/homebrew/opt/ncurses/lib/libpanelw.a",
               "-DLIBLZMA_INCLUDE_DIR=/opt/homebrew/opt/xz/include",
               "-DLIBLZMA_LIBRARY=/opt/homebrew/opt/xz/lib/liblzma.a",
               "-DLibEdit_INCLUDE_DIRS=" + str(deps / "include"),
               "-DLUA_LIBRARY=" + str(deps / "lib/liblua.a"),
               "-DLUA_INCLUDE_DIR=" + str(deps / "include"),
               "-DLUA_EXECUTABLE=" + str(deps / "bin/lua")]
    run(command, environment, volume / "logs/configure.log")
    return environment


def build_runtime(volume, environment, jobs, public=False):
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    cxx = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    flags = f"-ffile-prefix-map={PROJECT}=/__llvm-src"
    runtime = volume / ("public-runtimes" if public else "runtimes")
    log_prefix = "public-runtime" if public else "runtime"
    command = ["cmake", "-S", volume / "source/runtimes", "-B", runtime,
               "-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_C_COMPILER=" + compiler, "-DCMAKE_CXX_COMPILER=" + cxx,
               "-DCMAKE_OSX_SYSROOT=" + sdk, "-DCMAKE_OSX_DEPLOYMENT_TARGET=27.0",
               "-DCMAKE_C_FLAGS=" + flags, "-DCMAKE_CXX_FLAGS=" + flags,
               "-DCMAKE_INSTALL_PREFIX=/__llvm",
               "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind",
               "-DLLVM_ENABLE_LTO=" + ("OFF" if public else "Thin"),
               "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
               "-DLIBCXX_ENABLE_SHARED=" + ("ON" if public else "OFF"), "-DLIBCXX_ENABLE_STATIC=ON",
               "-DLIBCXXABI_ENABLE_SHARED=" + ("ON" if public else "OFF"), "-DLIBCXXABI_ENABLE_STATIC=ON",
               "-DLIBUNWIND_ENABLE_SHARED=" + ("ON" if public else "OFF"), "-DLIBUNWIND_ENABLE_STATIC=ON",
               "-DLIBCXX_INCLUDE_TESTS=OFF", "-DLIBCXXABI_INCLUDE_TESTS=OFF",
               "-DLIBUNWIND_INCLUDE_TESTS=OFF", "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
               "-DLIBCXX_INCLUDE_DOCS=OFF", "-DLIBCXXABI_INCLUDE_DOCS=OFF",
               "-DLIBCXX_ABI_VERSION=1", "-DLIBCXX_ABI_NAMESPACE=__1",
               "-DLIBCXX_HARDENING_MODE=none",
               "-DLIBCXX_ENABLE_VENDOR_AVAILABILITY_ANNOTATIONS=OFF"]
    run(command, environment, volume / ("logs/" + log_prefix + "-configure.log"))
    run(["cmake", "--build", runtime, "--target", "cxx", "cxxabi", "unwind",
         "--parallel", str(jobs)], environment,
        volume / ("logs/" + log_prefix + "-build.log"))


def build_packer(volume, environment):
    compiler = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    output = volume / "tools/llvm-pack"
    output.parent.mkdir(exist_ok=True)
    runtime = volume / "runtimes"
    run([compiler, "-std=c++23", "-O3", "-flto=thin", "-isysroot", sdk,
         "-nostdinc++", "-isystem", runtime / "include/c++/v1",
         "-I/opt/homebrew/include", "-nostdlib++", "-Wl,-exported_symbol,_main",
         "-ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src",
         PROJECT / "tools/pack.cpp", "/opt/homebrew/opt/zstd/lib/libzstd.a",
         runtime / "lib/libc++.a", runtime / "lib/libc++abi.a",
         runtime / "lib/libunwind.a", "-o", output], environment,
        volume / "logs/packer-build.log")


def build_cross_macos_runtime(volume, environment, jobs):
    """Cross-build only the x86-64 C++ runtime with the embedded compiler."""
    compiler = volume / "final/LLVM"
    if not compiler.is_file():
        raise RuntimeError("build a candidate before cross-building its C++ runtime")
    runtime = volume / "runtimes-x86_64-macos"
    sdk = volume / "overlay/root/targets/macos"
    flags = "-compile-target x86_64-macos -ffile-prefix-map=" + str(PROJECT) + "=/__llvm-src"
    command = ["cmake", "-S", volume / "source/runtimes", "-B", runtime,
               "-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_SYSTEM_NAME=Darwin", "-DCMAKE_OSX_ARCHITECTURES=x86_64",
               "-DCMAKE_OSX_SYSROOT=" + str(sdk), "-DCMAKE_OSX_DEPLOYMENT_TARGET=27.0",
               "-DCMAKE_C_COMPILER=" + str(compiler), "-DCMAKE_C_COMPILER_ARG1=cc",
               "-DCMAKE_CXX_COMPILER=" + str(compiler), "-DCMAKE_CXX_COMPILER_ARG1=c++",
               "-DCMAKE_ASM_COMPILER=" + str(compiler), "-DCMAKE_ASM_COMPILER_ARG1=cc",
               "-DCMAKE_C_FLAGS=" + flags, "-DCMAKE_CXX_FLAGS=" + flags,
               "-DCMAKE_ASM_FLAGS=" + flags,
               "-DCMAKE_AR=" + str(volume / "build/bin/llvm-ar"),
               "-DCMAKE_RANLIB=" + str(volume / "build/bin/llvm-ranlib"),
               "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
               "-DCMAKE_INSTALL_PREFIX=/__llvm/targets/x86_64-macos",
               "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind", "-DLLVM_ENABLE_LTO=OFF",
               "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DLIBCXX_ENABLE_SHARED=OFF",
               "-DLIBCXXABI_ENABLE_SHARED=OFF", "-DLIBUNWIND_ENABLE_SHARED=OFF",
               "-DLIBCXX_INCLUDE_TESTS=OFF", "-DLIBCXXABI_INCLUDE_TESTS=OFF",
               "-DLIBUNWIND_INCLUDE_TESTS=OFF", "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
               "-DLIBCXX_INCLUDE_DOCS=OFF", "-DLIBCXXABI_INCLUDE_DOCS=OFF",
               "-DLIBCXX_ABI_VERSION=1", "-DLIBCXX_ABI_NAMESPACE=__1",
               "-DLIBCXX_HARDENING_MODE=none", "-DLIBCXX_ENABLE_VENDOR_AVAILABILITY_ANNOTATIONS=OFF"]
    run(command, environment, volume / "logs/cross-macos-runtime-configure.log")
    run(["cmake", "--build", runtime, "--target", "cxx", "cxxabi", "unwind",
         "--parallel", str(jobs)], environment, volume / "logs/cross-macos-runtime-build.log")
    destination = volume / "overlay/root/targets/x86_64-macos/lib"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("libc++.a", "libc++abi.a", "libunwind.a"):
        target = destination / name
        if target.exists():
            target.unlink()
        os.link(runtime / "lib" / name, target)
    run(["codesign", "-s", "-", "--force", output], environment)


def build_compiler_runtime(volume, environment, jobs):
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    cxx = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    flags = f"-ffile-prefix-map={PROJECT}=/__llvm-src"
    runtime = volume / "compiler-rt"
    run(["cmake", "-S", volume / "source/compiler-rt", "-B", runtime,
         "-G", "Unix Makefiles", "-DCMAKE_BUILD_TYPE=Release",
         "-DCMAKE_C_COMPILER=" + compiler, "-DCMAKE_CXX_COMPILER=" + cxx,
         "-DCMAKE_OSX_SYSROOT=" + sdk, "-DCMAKE_OSX_DEPLOYMENT_TARGET=27.0",
         "-DCMAKE_C_FLAGS=" + flags, "-DCMAKE_CXX_FLAGS=" + flags,
         "-DLLVM_CMAKE_DIR=" + str(volume / "build/lib/cmake/llvm"),
         "-DLLVM_MAIN_SRC_DIR=" + str(volume / "source/llvm"),
         "-DCMAKE_INSTALL_PREFIX=/__llvm", "-DCOMPILER_RT_INCLUDE_TESTS=OFF",
         "-DCOMPILER_RT_BUILD_BUILTINS=ON", "-DCOMPILER_RT_BUILD_SANITIZERS=ON",
         "-DCOMPILER_RT_BUILD_XRAY=ON", "-DCOMPILER_RT_BUILD_LIBFUZZER=ON",
         "-DCOMPILER_RT_BUILD_PROFILE=ON", "-DCOMPILER_RT_BUILD_MEMPROF=ON",
         "-DCOMPILER_RT_ENABLE_IOS=OFF", "-DCOMPILER_RT_ENABLE_WATCHOS=OFF",
         "-DCOMPILER_RT_ENABLE_TVOS=OFF", "-DCOMPILER_RT_ENABLE_XROS=OFF",
         "-DDARWIN_osx_ARCHS=arm64;x86_64"], environment,
        volume / "logs/compiler-rt-configure.log")
    run(["cmake", "--build", runtime, "--parallel", str(jobs)], environment,
        volume / "logs/compiler-rt-build.log")


def build_final_candidate(volume, environment, reuse_pack=False):
    from build_native_bundles import recipes
    from rename_entry import rename_entry
    manifest = json.loads((volume / "bundle-manifest.json").read_text())
    required = recipes(volume / "build").keys() - {"llvm"}
    if manifest["version"] != RELEASE["version"] or manifest["failures"] or \
            required - set(manifest["completed"]):
        raise RuntimeError("complete all LLVM modules before final assembly")
    overlay = volume / "overlay"
    if not (overlay / "root/share/llvm-cli/components.json").is_file():
        raise RuntimeError("stage the updated SDK and runtime overlay before final assembly")
    if not (overlay / "root/targets/macos/usr/lib/libSystem.tbd").is_file():
        raise RuntimeError("stage the embedded Darwin resources before final assembly")
    script = overlay / "scripts/update"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROJECT / "install/update.py", script)
    certificates = overlay / "root/share/llvm-cli/cacert.pem"
    shutil.copyfile(PROJECT / "install/cacert.pem", certificates)
    shutil.copytree(PROJECT / "licenses", overlay / "root/share/licenses", dirs_exist_ok=True)
    image = volume / "final"
    entry = image / "bundles/llvm.bundle.objects/llvm-driver.cpp.o"
    entry.parent.mkdir(parents=True, exist_ok=True)
    build = volume / "build"
    link = build / "tools/CMakeFiles/llvm-driver.dir/link.txt"
    compiler = shlex.split(link.read_text())[0]
    rename_entry(build / "tools/CMakeFiles/llvm-driver.dir/llvm-driver/llvm-driver.cpp.o",
                 entry, "LLVM_llvm_main", PROJECT / "recovered/llvm-all/inmemory/LLVM",
                 build / "bin/llvm-objcopy", compiler)
    final_environment = dict(environment)
    final_environment.update({"LLVM_CLI_NATIVE_BUILD_DIR": str(build),
                              "LLVM_CLI_IMAGE_DIR": str(image),
                              "LLVM_CLI_PAYLOAD_DIR": str(PROJECT / "recovered/llvm-all/inmemory/payload"),
                              "LLVM_CLI_OVERLAY_DIR": str(overlay),
                              "LLVM_CLI_STDLIB_DIR": str(volume / "runtimes/lib"),
                              "LLVM_CLI_PACKER": str(volume / "tools/llvm-pack"),
                              "LLVM_CLI_LLVM_VERSION": RELEASE["version"],
                              "LLVM_CLI_LTO_CACHE_PERCENT": "1",
                              "ZSTD_INCLUDE_DIR": "/opt/homebrew/include", "CXX": compiler})
    command = [sys.executable, "-B", PROJECT / "tools/build_final.py"]
    if reuse_pack:
        command.append("--reuse-pack")
    run(command, final_environment,
        volume / "logs/final-build.log")
    print(f"Candidate: {image / 'LLVM'}; run full inventory and functional checks before installation.")


def retire_duplicates(volume):
    from build_native_bundles import recipes
    manifest_path = volume / "bundle-manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text())
    if manifest["version"] != RELEASE["version"]:
        raise RuntimeError("refusing to retire outputs from a different LLVM release")
    keep = {"llvm", "FileCheck", "llvm-config", "flang-23", "debugserver",
            "llc", "opt", "llvm-link", "llvm-as", "llvm-dis"}
    links = recipes(volume / "build")
    record_path = volume / "retired-duplicates.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    retired_bytes = 0
    for name in manifest["completed"]:
        if name in keep or "tblgen" in name or "Generator" in name or name.endswith("-gen"):
            continue
        compressed = volume / "overlay/bundles" / (name + ".bundle.zst")
        executable = volume / "build/bin" / name
        if name not in links or not compressed.is_file() or compressed.stat().st_size == 0 \
                or executable.is_symlink() or not executable.is_file():
            continue
        # Only fresh standalone outputs in the capped workspace are eligible.
        with executable.open("rb") as stream:
            if stream.read(4) != b"\xcf\xfa\xed\xfe":
                raise RuntimeError("unexpected standalone output format: " + name)
        size = executable.stat().st_size
        executable.unlink()
        record[name] = {"bytes": size, "target": links[name].parent.stem}
        retired_bytes += size
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Retired {retired_bytes / 1024 ** 2:.1f} MiB of duplicate standalone outputs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true",
                        help="also compile the multicall core after preparation")
    parser.add_argument("--status", action="store_true",
                        help="report workspace usage and recent build progress without changing files")
    parser.add_argument("--runtime", action="store_true",
                        help="build matched static C++ runtime libraries from the pinned source")
    parser.add_argument("--packer", action="store_true",
                        help="build the static payload packer using the matched runtime")
    parser.add_argument("--compiler-runtime", action="store_true",
                        help="build macOS compiler runtimes for arm64 and x86_64")
    parser.add_argument("--public-runtime", action="store_true",
                        help="build native C++ runtime archives consumable by upstream LLD")
    parser.add_argument("--cross-macos-runtime", action="store_true",
                        help="use the candidate CLI to cross-build x86-64 macOS C++ runtimes")
    parser.add_argument("--final", action="store_true",
                        help="assemble the complete candidate after module and SDK staging")
    parser.add_argument("--reuse-pack", action="store_true",
                        help="resume final linking with an unchanged completed payload pack")
    parser.add_argument("--retire-duplicates", action="store_true",
                        help="remove regenerated standalone outputs already stored as embedded modules")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("this build entry point is for macOS")
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    if args.reuse_pack and not args.final:
        parser.error("--reuse-pack requires --final")
    if args.status:
        status()
        return
    volume = workspace()
    if args.retire_duplicates:
        retire_duplicates(volume)
        return
    prepare(volume)
    auxiliary = args.runtime or args.packer or args.compiler_runtime or args.public_runtime or args.cross_macos_runtime or args.final
    environment = (build_environment(volume) if auxiliary and not args.build
                   else configure(volume))
    if args.build:
        run(["cmake", "--build", volume / "build", "--target", "llvm-driver",
             "clang-resource-headers",
             "--parallel", str(args.jobs)], environment, volume / "logs/core-build.log")
    if args.runtime:
        build_runtime(volume, environment, args.jobs)
    if args.packer:
        build_packer(volume, environment)
    if args.compiler_runtime:
        build_compiler_runtime(volume, environment, args.jobs)
    if args.public_runtime:
        build_runtime(volume, environment, args.jobs, public=True)
    if args.cross_macos_runtime:
        build_cross_macos_runtime(volume, environment, args.jobs)
    if args.final:
        build_final_candidate(volume, environment, args.reuse_pack)
    print(f"Workspace: {volume}", flush=True)
    if not args.final:
        print("Core build is an intermediate artifact; install only after the full "
              "embedded tool inventory and runtime checks pass.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"LLVM-CLI build: {error}", file=sys.stderr)
        raise SystemExit(1)
