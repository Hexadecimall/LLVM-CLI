#pragma once

#include <functional>
#include <string>
#include <string_view>
#include <vector>

using BuildInvoke = std::function<int(
    std::string_view compiler, std::string_view target,
    const std::vector<std::string> &arguments)>;

int runIncrementalBuild(int argc, char **argv, int firstArgument,
                        std::string_view executable,
                        const BuildInvoke &invoke);
