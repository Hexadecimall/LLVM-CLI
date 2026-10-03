# Third-party notices

The LLVM project and LLVM-CLI's original code use Apache-2.0 WITH
LLVM-exception; see [LICENSE](LICENSE). The recovered build and packaged
payload include additional upstream components. Their original notice texts
are preserved under `licenses/`:

| Component | Notice |
| --- | --- |
| Python | `licenses/python/LICENSE` |
| bzip2 | `licenses/bzip2/LICENSE` |
| cpp-httplib | `licenses/cpp-httplib/LICENSE` |
| curl | `licenses/curl/COPYING` |
| libedit | `licenses/libedit/COPYING` |
| libffi | `licenses/libffi/LICENSE` |
| GNU libiconv | `licenses/libiconv/COPYING`, `COPYING.LIB` |
| libxml2 | `licenses/libxml2/Copyright` |
| Lua | `licenses/lua/LICENSE` |
| SWIG | `licenses/swig/LICENSE`, `LICENSE-GPL`, `LICENSE-UNIVERSITIES`, `COPYRIGHT` |
| tree-sitter | `licenses/tree-sitter/LICENSE` |
| Z3 | `licenses/z3/LICENSE.txt` |
| zlib | `licenses/zlib/LICENSE` |
| Zig-provided target runtimes | `licenses/zig/LICENSE` |
| libc++, libc++abi, libunwind | `licenses/libcxx/`, `libcxxabi/`, `libunwind/` |
| zstd | `licenses/zstd/LICENSE` |

This is a source-level notice inventory, not a completed release audit.
Before distributing a new single-file image, verify the exact embedded
component list and include all applicable notices in that image or its
distribution. `tools/audit_public.py` checks local identifiers; it does not
replace a license review.
