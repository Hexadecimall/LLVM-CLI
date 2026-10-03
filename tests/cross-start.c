#if defined(_WIN32)
void mainCRTStartup(void) {
  for (;;) {}
}
#else
void _start(void) {
  for (;;) {}
}
#endif
