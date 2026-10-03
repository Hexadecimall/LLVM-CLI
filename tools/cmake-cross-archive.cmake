# Use the single LLVM-CLI binary's format-aware ar and ranlib aliases.
if(NOT LLVM_CLI_BUILD_TOOLS_DIR)
  message(FATAL_ERROR "LLVM_CLI_BUILD_TOOLS_DIR is required")
endif()
set(CMAKE_AR "${LLVM_CLI_BUILD_TOOLS_DIR}/llvm-ar")
set(CMAKE_RANLIB "${LLVM_CLI_BUILD_TOOLS_DIR}/llvm-ranlib")
