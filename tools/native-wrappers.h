#pragma once

#include <string>

bool isNativeWrapper(const std::string &name);

// Returns -1 when the command is not handled here.
int runNativeWrapper(const std::string &name, int argc, char **argv,
                     int firstArgument);
