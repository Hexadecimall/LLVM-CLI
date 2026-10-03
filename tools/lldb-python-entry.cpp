// Python entrypoint for the embedded LLDB module. Keeping this beside LLDB's
// built-in _lldb initializer ensures both use the same statically linked
// Python runtime.

struct _object;
using PyObject = _object;
using PyInitFunction = PyObject *(*)();

extern "C" PyObject *PyInit__lldb();
extern "C" int PyImport_AppendInittab(const char *, PyInitFunction);
extern "C" int Py_BytesMain(int, char **);

extern "C" __attribute__((visibility("default")))
int LLVM_lldb_python_main(int argc, char **argv) {
  if (PyImport_AppendInittab("_lldb", PyInit__lldb) != 0)
    return 1;
  return Py_BytesMain(argc, argv);
}
