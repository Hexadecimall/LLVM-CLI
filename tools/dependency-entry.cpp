#ifndef DEPENDENCY_ENTRY
#error DEPENDENCY_ENTRY must name the exported tool entrypoint
#endif

#ifndef DEPENDENCY_MAIN
#error DEPENDENCY_MAIN must name the renamed dependency main
#endif

extern "C" int DEPENDENCY_MAIN(int argc, char **argv);

#ifdef DEPENDENCY_PYTHON_SCPROXY
struct _object;
using PyObject = _object;
extern "C" PyObject *PyInit__scproxy();
extern "C" int PyImport_AppendInittab(const char *, PyObject *(*)());
#endif

extern "C" __attribute__((visibility("default"))) int
DEPENDENCY_ENTRY(int argc, char **argv) {
#ifdef DEPENDENCY_PYTHON_SCPROXY
  static const int registered =
      PyImport_AppendInittab("_scproxy", PyInit__scproxy);
  if (registered != 0)
    return 1;
#endif
  return DEPENDENCY_MAIN(argc, argv);
}
