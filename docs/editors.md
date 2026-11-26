# Editor integration

The embedded `clangd` speaks the standard Language Server Protocol. Run
`llvm clangd --version` to confirm it is available; editors launch
`llvm clangd` as a subprocess for language-server communication. LLVM-CLI
does not launch an external clangd executable.

## Neovim 0.11+

Add this to `init.lua` (or an equivalent Neovim Lua configuration):

```lua
-- Read virtual headers directly from the executable when following definitions.
vim.api.nvim_create_autocmd('BufReadCmd', {
  pattern = '/__llvm/*',
  callback = function(ev)
    local path = vim.api.nvim_buf_get_name(ev.buf)
    local result = vim.system({ 'llvm', 'python', '-c',
      'import sys; sys.stdout.buffer.write(open(sys.argv[1], "rb").read())', path
    }, { text = true }):wait()
    if result.code ~= 0 then error(result.stderr) end
    local lines = vim.split(result.stdout, '\n', { plain = true })
    local eol = result.stdout:sub(-1) == '\n'
    if eol then table.remove(lines) end
    vim.bo[ev.buf].modifiable = true
    vim.api.nvim_buf_set_lines(ev.buf, 0, -1, false, lines)
    vim.bo[ev.buf].endofline = eol
    vim.bo[ev.buf].buftype = 'nofile'
    vim.bo[ev.buf].bufhidden = 'hide'
    vim.bo[ev.buf].modified = false
    vim.bo[ev.buf].readonly = true
    vim.bo[ev.buf].modifiable = false
    vim.api.nvim_exec_autocmds('BufReadPost', { buffer = ev.buf, modeline = false })
  end,
})

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
The read handler displays embedded headers as read-only buffers without
extracting an SDK to disk. Its Python interpreter runs inside the same
`llvm` executable. Project source buffers retain their normal file handling.

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
their source file. Darwin libc headers, native C++ headers, and Clang's resource
headers are embedded in LLVM-CLI. The embedded clangd selects the virtual
compiler directly instead of launching `xcrun` to discover an external compiler.
Apple framework headers require an explicitly selected framework SDK. Cross-target header
navigation needs a compile database or `.clangd` with that target's actual
triple and include paths.

Other LSP clients can use `llvm clangd` as their server command with the
same project compilation database.
