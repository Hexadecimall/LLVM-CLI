# Editor integration

The embedded `clangd` speaks the standard Language Server Protocol. Run
`llvm clangd --version` to confirm it is available; editors launch
`llvm clangd` as a subprocess for language-server communication. LLVM-CLI
does not launch an external clangd executable.

## Neovim 0.11+

Add this to `init.lua` (or an equivalent Neovim Lua configuration):

```lua
vim.lsp.config('llvm_clangd', {
  cmd = { 'llvm', 'clangd', '--background-index' },
  filetypes = { 'c', 'cpp', 'objc', 'objcpp', 'cuda' },
  root_markers = { 'compile_commands.json', 'compile_flags.txt', '.clangd', '.git' },
})
vim.lsp.enable('llvm_clangd')
vim.keymap.set('n', 'gd', vim.lsp.buf.definition, { desc = 'Go to definition' })
```

Open a C or C++ file and use `gd` on an included symbol. `:LspInfo` shows
whether `llvm_clangd` attached. `llvm clangd --check=path/to/file.cpp`
is a quick command-line diagnostic when a header cannot be found.

`clangd` gets project-specific include directories, defines, language
standards, and target flags from `compile_commands.json` or `.clangd`.
For CMake projects, enable `CMAKE_EXPORT_COMPILE_COMMANDS` and place a
symlink to the resulting database at the project root, or set
`CompileFlags.CompilationDatabase` in `.clangd` to the build directory.
For a small project without a database, a root `.clangd` can specify:

```yaml
CompileFlags:
  Add: [-std=c++17, -Iinclude]
```

Use the project's real flags. `-Iinclude` is only an example; it does not
discover arbitrary headers. Quoted local includes are resolved relative to
their source file. macOS system headers still come from the active SDK,
while Clang's resource headers are embedded in LLVM-CLI. Cross-target header
navigation needs a compile database or `.clangd` with that target's actual
triple and include paths.

Other LSP clients can use `llvm clangd` as their server command with the
same project compilation database.
