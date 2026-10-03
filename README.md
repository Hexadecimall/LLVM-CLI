# LLVM-CLI

Host status: the current single-file executable runs on macOS/Apple Silicon.
Its cross-compilation targets produce Linux, Windows, and WebAssembly output;
they do **not** make this Mach-O executable itself run on those hosts. Native
Linux and Windows editions require host-specific rebuilds of the container and
embedded LLVM tools. See [Portability](PORTABILITY.md) for the remaining work
and the disk-space constraints.

`LLVM` is an in-process LLVM multicall command. The installed `LLVM` and
`llvm` names refer to the same binary. Tools, headers, and packaged runtimes
are read from the binary's virtual `/__llvm` root.
LLVM-prefixed tools accept the shorter command name too: `llvm nm`,
`llvm ar`, and `llvm objdump` invoke their embedded `llvm-*` counterparts.
For Neovim and other LSP editors, use the embedded `llvm clangd`; see
[Editor integration](docs/editors.md).

## Install from a release

Install the native macOS/Apple Silicon edition with:

```sh
curl -fsSL https://raw.githubusercontent.com/Hexadecimall/LLVM-CLI/main/install/install.sh | sh
```

On Windows, once a native Windows edition exists, the corresponding command is:

```powershell
irm https://raw.githubusercontent.com/Hexadecimall/LLVM-CLI/main/install/install.ps1 | iex
```

Inspect a remote installer before piping it into a shell. The scripts pin the
resolved release tag, verify every downloaded part and the assembled binary
against the release manifest's SHA-256 hashes, then install one executable.
If the installed executable already matches the release size and SHA-256, the
installer skips the binary downloads entirely.
These hashes detect transfer errors and tampering relative to the manifest;
they are not an independent publisher signature. The macOS installer also
checks the executable's code signature. Set `LLVM_CLI_VERSION` to a release
tag or `LLVM_CLI_INSTALL_DIR` to an absolute directory to override defaults.
The shell script also accepts `--version`, `--install-dir`, and `--force`
when downloaded and run as a file; PowerShell accepts `-Version`,
`-InstallDir`, and `-Force`. An existing unrelated `llvm` is not replaced
unless replacement is explicitly forced.

The macOS installer writes one lowercase `llvm` executable; an uppercase
`LLVM` alias is added only on case-sensitive filesystems. Windows installs
`llvm.exe`. No native Linux or Windows
LLVM-CLI release is available yet. Cross-compiled output is not a substitute
for a host-native CLI.

For release preparation, run `python3 tools/prepare_release.py --output-dir
release-assets --binary darwin-arm64=PATH_TO_NATIVE_LLVM` and upload every
file in `release-assets` to the same GitHub Release. `ghx api` creates and
publishes the release; `python3 tools/upload_release_asset.py RELEASE_ID FILE`
streams each asset using the existing `ghx` credential without printing or
storing it. Keep the release in draft until every uploaded size and digest has
been checked against the manifest. The release packer audits source and binary
paths before packaging. It splits the current 2.6 GB macOS binary into 1 GiB
assets because a GitHub Release asset must be under 2 GiB.
Add Linux or Windows binaries only after those editions are built and verified
on their native hosts.

## Cross compilation

The command form is `llvm <compiler> -compile-target <target> [arguments]`.
`-compile-target` accepts Zig-style `arch-os-abi` names; `native` selects the
host target. Run `llvm extras [tool]` to see LLVM-CLI commands and added flags.
Quoted Ghidra processor IDs from `llvm compile-targets ghidra` are also
accepted for verified freestanding object/assembly targets:

```sh
llvm cc -compile-target 'MIPS:BE:32:default' source.c -c -o source.o
llvm cc -compile-target 'RISCV:LE:64:RV64GC' source.c -S -o source.s
```

These Ghidra IDs do not include an OS, ABI, C runtime, or linker environment.
LLVM-CLI reports listed profiles without a verified LLVM code-generation
mapping as unsupported; they are not silently reduced to a generic CPU.

```sh
llvm cc -compile-target x86_64-linux-musl source.c -c -o source.o
llvm c++ -compile-target aarch64-linux-gnu source.cpp -c -o source.o
llvm fortran -O2 -compile-target riscv64-linux-musl source.f90 -c -o source.o
llvm llc -compile-target wasm32-wasi module.ll -filetype=obj -o module.o
llvm cc -compile-target x86_64-macos source.c -o app
llvm cc -compile-target x86_64-linux-musl source.c -o app
llvm cc -compile-target x86_64-linux-musl -dynamic source.c -o dynamic-app
llvm c++ -compile-target x86_64-linux-musl source.cpp -o app
llvm cc -compile-target aarch64-linux-musl source.c -o app
llvm cc -compile-target aarch64-linux-musl -dynamic source.c -o dynamic-app
llvm cc -compile-target x86_64-linux-gnu -fPIC -shared source.c -o library.so
llvm cc -compile-target wasm32-wasi source.c -o app.wasm
llvm cc -compile-target wasm32-wasi -fPIC -shared source.c -o module.wasm
llvm cc -compile-target x86_64-windows-gnu source.c -o app.exe
llvm cc -compile-target x86_64-windows-gnu -shared source.c -o library.dll
llvm ld --version
llvm lld --version
llvm lld -flavor gnu --version
llvm wasm-ld --version
```

`llvm ld` and `llvm lld` use the embedded LLD, never `/usr/bin/ld` or another
external linker. They infer ELF, Mach-O, COFF, or WebAssembly from input object
files and recognized linker flags. Use `-flavor gnu`, `-flavor darwin`,
`-flavor link`, or `-flavor wasm` when inputs are ambiguous (such as bitcode or
an archive alone). The format-specific commands `ld.lld`, `ld64.lld`,
`lld-link`, and `wasm-ld` remain available directly.

Clang also covers Objective-C, Objective-C++, and assembly via `-x`; `llc`
accepts LLVM IR. Run `llvm compile-targets` for the installed support summary.

The LLVM backends produce assembly and objects for their supported CPUs and
object formats. macOS executable linking uses the embedded linker and the
machine's Apple SDK. The x86-64 and AArch64 Linux musl targets include Zig's
libc headers, startup objects, libc, and compiler runtimes; normal C programs
link as static executables. The x86-64 Linux musl target also embeds libc++
headers, libc++abi, and libunwind for static C++ standard-library linking.
Linux shared libraries link without pulling in the static musl archive; libc
symbols they reference must be supplied by the target system at load time.
The two embedded Linux musl targets also include a shared libc and PIE startup
object. `-dynamic` selects a dynamically linked executable with the target's
musl interpreter path; that loader and `libc.so` must exist on the machine
running the output. Other Linux targets need `--sysroot` with a compatible
dynamic libc and loader for dynamically linked executables.
The WASI target includes the corresponding libc
and links `.wasm` commands. `-shared` creates an experimental WebAssembly
shared module; loading and resolving its imports depend on the WASM host.
For other systems, an executable can be linked with
`-nostdlib` when the program provides its own entry point and runtime;
alternatively, pass an explicit `--sysroot` with the target's runtime. The
compiler reports the missing runtime before attempting a normal link.

Windows GNU includes Zig's MinGW headers, COFF startup objects, and UCRT/Win32
import libraries. It produces console executables and DLLs that dynamically
import the appropriate Windows system libraries. The LLVM-CLI binary itself
continues to link only to macOS `libSystem`.
Because this embedded Windows runtime imports system DLLs, Windows `-static`
links are rejected rather than mislabeled as fully static. An import-free
freestanding PE requires `-nostdlib` and an explicit entry point.

On the native Apple Silicon host, `llvm c++` uses the embedded static libc++,
libc++abi, and libunwind archives by default; a normal C++ executable imports
only `libSystem`. For an explicit `-nodefaultlibs`/`-nostdlib` macOS link,
LLVM-CLI still adds `libSystem`, which macOS requires to load the executable.
Clang's default C++ language mode is C++17 (`__cplusplus=201703L`); select
C++26 with `-std=c++26` (`__cplusplus=202400L` in Clang 23).

Windows C++ standard-library headers and libraries are not yet embedded, so
`llvm c++ -compile-target x86_64-windows-gnu` is not a complete hosted C++
toolchain. C and freestanding C++ remain available.

`-compile-target` selects the target for Clang, Flang, or `llc`. Flang 23 has
target-specific lowering for fewer architectures than Clang, so some valid
Clang targets cannot compile Fortran.

Common LLVM triples such as `x86_64-unknown-linux-musl` are accepted as
aliases of `x86_64-linux-musl`. `linux-arm64` selects the embedded
`aarch64-linux-musl` target. Run `llvm compile-targets TARGET` to see a
normalization. A GNU/Linux target such as `x86_64-unknown-linux-gnu` is not
silently changed into musl: executable linking needs a matching glibc
`--sysroot`. `-target` remains Clang's raw triple flag; use either it or
`-compile-target`, not both.

## Incremental builds

`llvm build` compiles C, C++, Objective-C, Objective-C++, and assembly sources
with the same embedded Clang and linker. It keeps objects and dependency files
in a durable `.llvm-cli-build` directory in the current project, checks local
header dependencies, and links again only when inputs or flags changed:

```sh
llvm build --output app src/main.cpp src/helper.cpp
llvm build --target x86_64-linux-musl --output app-linux src/main.c
llvm build --release --output app src/main.cpp --cflag=-Iinclude --ldflag=-lm
```

Use `--build-dir PATH` to choose another project-local build directory, and
add `.llvm-cli-build/` to the project's `.gitignore`. `--release` enables
`-O3 -flto=thin`; debug-oriented builds default to `-O0`. `--cflag` and
`--ldflag` are repeatable. External libraries remain explicit: adding a
header search path does not link its library. For example, Homebrew fmt needs
`--ldflag=-lfmt` and, if outside the default search path, a suitable
`--ldflag=-L...`; `-DFMT_HEADER_ONLY` is an alternative when fmt's header-only
mode is appropriate. This build command does not yet orchestrate Fortran.

## Source and licensing

LLVM-CLI's original source uses Apache-2.0 WITH LLVM-exception; see
[LICENSE](LICENSE). Bundled upstream components retain their own licenses,
collected under [licenses](licenses). The source tree deliberately excludes
recovered toolchains, object files, generated images, and local test output.
The upstream changes needed for embedded-header lookup in `clangd` live in
[`patches/llvm-embedded-vfs.patch`](patches/llvm-embedded-vfs.patch) and
[`patches/clangd-embedded-resource-dir.patch`](patches/clangd-embedded-resource-dir.patch).
Apply both to the LLVM 23.1.1 source tree before rebuilding those components.
Run `python3 tools/audit_public.py` before publishing source; pass a release
binary path as an argument to check it for local home/project paths too.
