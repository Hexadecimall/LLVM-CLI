# Native Linux and Windows editions

`-compile-target` controls the output of an embedded compiler, not the host
format of LLVM-CLI. The current installed host container is Mach-O and obtains
its embedded pack from a Mach-O section. Its in-process tool modules are
Mach-O bundles loaded with dyld APIs. Neither module format nor loader works
in an ELF or PE executable.

The source container now has an ELF pack-symbol path and a Linux memory-backed
module loader using `memfd_create` and `dlopen`. LLVM-CLI itself compiled
`tools/llvm-memory.cpp` to a Linux ELF object. LLVM-CLI also converted a
small resource to an ELF object and compiled the same resource into a PE
`.res` file. Those are host-port building blocks, not a runnable native
edition: the LLVM tool modules and their dependencies must still be rebuilt
for each host. The Windows container loader and PE pack access remain to be
implemented.

Darwin libc headers, `libSystem` text stubs, Clang-compatible target metadata,
and Apple Silicon/Intel macOS C++ archives are now staged as embedded target
data. Ordinary macOS C/C++ cross-compilation can reuse these resources when
native Linux and Windows containers become available; it need not discover an
Xcode installation on those hosts. Apple framework SDKs are outside this
libc resource set. This target-data work does not implement the missing host
container or module loaders.

A native edition needs the same source toolchain built for its host, with
platform-specific pack-section access, in-memory module loading, path/file
interposition, and dependency closure. The existing macOS object archives
cannot simply be relinked into ELF or PE. Linux ELF modules could use an
in-memory loader; Windows PE modules need their own equivalent. Both must
avoid shelling out or extracting executables to disk.

The size rule is strict: keep one recovered upstream source tree, one staged
set of target headers/runtimes per genuinely supported target, and reuse
objects/artifacts where the format permits. Build one host edition at a time.
Do not clone the 66 GB recovered LLVM build tree or create another 70 GB
parallel tree. `llvm` can cross-compile portable project sources today, but
that alone cannot convert macOS LLVM objects to Linux/Windows objects.

Current verification:

- The installed CLI linked C source into static Linux ELF, dynamic Linux
  ELF, Windows PE, and WASI executables. File format and dependency tables
  were checked; cross-host execution was not available on this Mac.
- Native macOS C++ and mixed assembly/C++ executables were built and run.
  Both depended only on `libSystem` after static libc++ linking.
- The installed `llvm c++ -compile-target x86_64-linux-musl` compiled
  `tools/pack.cpp` to an ELF object with an empty `PATH`.
- The same compiler cross-compiled `tools/llvm-memory.cpp`, including its
  Linux in-memory loader, to an ELF object.
- The same CLI compiled `tools/dependency-entry.cpp` to a Windows COFF C++
  object, also with an empty `PATH`; this source does not require libc++.
- The Windows C++ equivalent is blocked by the absent embedded libc++
  headers and archives; external headers alone were insufficient without
  target-specific libc++ configuration.
- No native Linux or Windows LLVM-CLI executable has been built or run yet.

The current host bootstrap shares the LLVM 23.1.6 source with the macOS build.
Idle generated macOS inputs can be frozen with verified Zstandard frames to
reserve space for another host without deleting its incremental build inputs.
`tools/prepare_linux_runtime.py` reuses installed Zig glibc headers, import
stubs, and minimal C startup code, then builds LLVM's C++ runtime using
LLVM-CLI. `tools/linux_build.py` cross-builds loader libraries with that
runtime and the existing native table generators. Its initial library-only
profile disables optional compression/XML dependencies; it is a bootstrap
experiment, not a release profile or complete tool inventory.

`tools/prepare_windows_runtime.py` builds Windows libc++, libc++abi, and
libunwind against the retained C headers and import archives. The Linux and
Windows C++ probes link with static C++ runtimes and system C runtime imports;
`tools/check_host_runtime.py` verifies those dependency lists. This does not
yet add automatic GNU/Linux or Windows hosted C++ resources to the installed
macOS edition.

`tools/probe_object_loader.py` verifies an alternative module representation:
precompiled objects and LLVM's existing ORC platform runtime embedded together
in one executable. The macOS probe runs constructors, per-thread TLS, an entry
point, and destructors without external tools at execution time. Linux and
Windows implementations still require native validation before publication.
