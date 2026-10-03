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
