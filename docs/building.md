# Development and storage

## LLVM 23.1.6 macOS rebuild

The release tag and source digest are pinned in `config/llvm-release.json`.
Run `make macos-core` (or `python3 -B tools/macos_build.py --build --jobs 4`) to prepare and compile
the native multicall core. This is an intermediate build, not an installer:
the complete embedded tool inventory must pass before replacing the command.
`config/required-tools.txt` records the existing command names. Run
`python3 -B tools/check_tool_inventory.py PATH_TO_LLVM` on the packaged command
to catch missing names, then run functional smoke tests; a list alone is not
evidence that every command works.
Use `make macos-status` to inspect workspace allocation and recent progress
without starting or interrupting a build.

The workspace is a durable 24 GiB sparse APFS disk image under
`build-work/macos-23.1.6/`. Downloads, candidate source, objects, logs, temporary
files, and caches stay inside its mounted `volume/` directory. Image capacity
leaves headroom below the 25 GiB additional-storage budget. Keep at least
40 GiB free before starting. The installed binary is not replaced by this script.

Source preparation verifies the official source SHA-256, preserves local
embedding patches against the retained baseline archive, and reapplies them
to the new release. A tracked embedding patch also allows preparation when
the baseline source is unavailable. Unchanged source files are retained; an APFS clone is used
when source and destination share a filesystem. The capped image is a separate
filesystem, so it needs one ordinary source copy. Existing dependency sources
and archives are reused outside the workspace rather than rebuilt or staged
again. Patches that do not apply stop preparation for review.

`config/macos-release.cmake` retains production projects and all stable and
experimental backends, uses Release/O3 and ThinLTO, and avoids building the
upstream test suites, examples, benchmarks, and documentation. It builds
static libraries with LLVM's multicall-driver support enabled. The retained
23.1.1 objects are not substituted into 23.1.6's LLVM core.
Existing dependencies were built for macOS 27, so this reuse configuration
targets macOS 27 rather than claiming compatibility with older releases.

After the core, `make macos-runtime` builds matching static libc++, libc++abi,
and libunwind archives. `make macos-packer` links the payload helper against
those archives, exporting only its entry point so its C++ symbols do not
interpose on system libraries. `make macos-bundles` rebuilds the retained LLVM
tool inventory using the same native objects and archives. Each module is
compressed immediately; successful modules and per-tool logs survive an
interrupted run under the mounted workspace. Independent dependency modules
retain their own versions and are reused.

The packer accepts an optional overlay directory. Overlay paths replace the
same baseline paths; `.pack-exclude` lists exact baseline paths to omit.
Modules compressed with `llvm-pack --compress input output.bundle.zst` are
stored as ordinary `.bundle` entries without recompression. Unchanged baseline
resources are read directly, avoiding a second full payload staging tree.
The source, core, runtimes, and module stages do not install a replacement.
Check the completed command with `tools/check_tool_inventory.py` and functional
tests before installation; an advertised command name alone is not a smoke test.

Continue with `make macos-public-runtime` and `make macos-compiler-runtime` for
native C++ archives, matching shared C++ libraries, and macOS compiler runtimes.
`make macos-sdk` builds missing
SDK libraries and stages the updated headers, libraries, Fortran runtime and
modules, and license notices. Flang's runtime uses the newly built compiler tools
in a separate incremental directory; the existing compiler-rt build is reused.
Staging requires every embedded LLVM tool module to be complete.
Before final assembly, reuse an installed Zig 0.17.0 library directory with
`make darwin-resources ZIG_LIB_DIR=/path/to/zig/lib`. This stages only its
Darwin libc headers, `libSystem.tbd`, and target metadata (about 7 MiB),
preserving the source notices and recording per-file hashes. It does not copy
Zig's executable or a complete Xcode SDK. Final assembly also embeds the
updater script and its CA certificate bundle from `install/`.
After the first candidate, `python3 -B tools/macos_build.py --cross-macos-runtime`
uses that CLI to cross-build only x86-64 libc++, libc++abi, and libunwind from
the same LLVM source, then stages their archives without copying them.
Reassemble the final candidate to embed these additional target runtimes.
`make macos-final` then produces the candidate at
`build-work/macos-23.1.6/volume/final/LLVM` without replacing the installed command.
If final linking was interrupted after packing and the payload has not changed,
`python3 -B tools/macos_build.py --final --reuse-pack` reuses that completed pack.
The linker strips local symbols directly, and the pack is retired before code
signing to avoid retaining another executable-sized temporary copy.

The application and tool modules use O3 and ThinLTO. Public SDK archives contain
native object members because upstream LLD cannot consume Xcode's private stack
probing attributes in its LTO bitcode. The archive preparation step reuses the
compiled bitcode, preserves member boundaries, and uses Xcode to emit native
code. The smaller public C++ runtimes have a separate native archive build.
SDK consumers can use the virtual include directory from `llvm llvm-config`.

LLDB retains its Python, Lua, terminal UI, LZMA, XML, and Tree-sitter features.
Static ncurses/panel, LZMA, and Tree-sitter archives are reused from the existing
package installation. The build supplies SWIG's relocated library directory
explicitly, keeping the retained generator usable after its original cache
location has gone away. Platform frameworks remain system dependencies of
tools that use them.

`make macos-status` reports the mounted filesystem's usage and the sparse image's
allocated bytes separately. Do not add the mounted directory's `du` result to
the image file's allocation: that counts the same storage twice. Tool-module
links request an LTO cache budget of 5% of available workspace space; final
assembly requests 1%. The linker prunes this cache during linking. After a module
is packed, `python3 -B tools/macos_build.py --retire-duplicates` can retire its
new standalone executable. Compilers, host generators, objects, libraries, and
the embedded module remain available. A record of retired targets is kept in
the workspace.

To retry packaging with existing objects, use
`python3 -B tools/build_native_bundles.py --relink-only --jobs 4`.
Missing native link inputs still trigger the necessary target build. Normal
module builds also check source and link-input timestamps before reusing an
existing compressed module. Manifests and compressed modules are replaced
atomically so interrupted packaging can resume.

The embedded Mach-O linker currently does not implement fully static macOS
executables. Explicit static executable requests fail instead of silently
adding a dynamic system runtime. Linux musl executable output is static by
default. `llvm licenses [component]` reads the notices carried in the executable.

## Existing build commands

Run `make` to list the development commands. `make check` checks the native
macOS link inputs without compiling, packing, or modifying the payload.
`make build` runs the same check before any build work. `make test` uses the
existing project executable; override it with `LLVM_CLI_BINARY=/path/to/llvm`.
`make audit` checks public source for private paths and credential patterns.

## Project layout

| Location | Purpose | Retention |
| --- | --- | --- |
| `tools/`, `patches/` | Container, embedded wrappers, packer, build scripts, upstream patches | Source |
| `tests/`, `install/`, `licenses/`, `docs/` | Tests, installers, notices, documentation | Source |
| `recovered/piper-llvm/llvm-project-23.1.1.src/` | Shared upstream LLVM source | Required to regenerate LLVM builds |
| `recovered/llvm-all/build/` | Native LLVM objects, static archives, and link recipes | Required for native relinking |
| `recovered/llvm-all/deps*`, `build-*` | Third-party sources, headers, archives, build objects | Preserve for dependency rebuilds |
| `recovered/llvm-all/inmemory/payload/` | Staged tool modules, headers, runtimes | Required to pack the executable |
| `recovered/llvm-all/inmemory/bundles/` | Tool modules and rewritten entry-point objects | Preserve, including `llvm.bundle.objects/llvm-driver.cpp.o` |
| `recovered/llvm-all/inmemory/objects/` | Container objects produced by `make build` | Generated; rebuilding them needs the inputs above |
| `recovered/llvm-all/inmemory/LLVM` | Current complete macOS command | Keep until a replacement is verified |
| `recovered/llvm-all/install/`, `stage/` | Native tools, archives, headers, dependency staging | Preserve while reconstructing native build inputs |
| `host-build/` | Linux build objects, archives, ThinLTO cache, and host archivers | Preserve for incremental Linux work |
| `release-assets/<version>/` | Verified release packages | Keep until the corresponding release is complete |
| `validation/`, `tests/build/`, `.llvm-cli-build/` | Test results and incremental objects | Generated; removing objects loses reuse |

Keep generated outputs inside the project, rather than system temporary or
cache directories. New container objects belong under `inmemory/objects/`,
not alongside source in `tools/`. The final pack and executable are staged as
`LLVM-memory.next.pack` and `LLVM.next`; a successful build replaces `LLVM`
and removes the intermediate pack.

## Legacy native build inputs

The native `recovered/llvm-all/build/` directory is currently absent from the
local recovered workspace. The existing command can run, tests can use it,
and release assets can be generated from it. The legacy `make build` command
needs that old directory. The `macos-*` commands above instead use the
regenerated 23.1.6 objects under the capped workspace; they do not require
restoring the old native build directory.
The upstream embedding patches are in `patches/`; restoring a generic LLVM
installation alone does not restore those build objects and recipes.

The build scripts report missing inputs before compiling or changing payloads.
Bundle generation also refuses an empty input set, preserving its existing
manifest. Do not substitute the Linux build directory: its objects have a
different executable format.

## Cleanup policy

An ignored directory is not automatically disposable. Keep one shared source
tree and reuse existing archives, objects, headers, and runtimes. In
particular, do not blanket-delete `recovered/`, `host-build/`, or `bundles/`.
The host archivers must remain runnable on the build machine.

Before deleting generated results, check running builds and uploads, identify
the exact paths and sizes, and distinguish build inputs from release packages
and old snapshots. Remove only the approved batch, then measure actual free
space; APFS clones and hard links can make apparent sizes misleading. This
project deliberately has no blanket `make clean` target.
