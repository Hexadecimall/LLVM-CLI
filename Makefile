PYTHON ?= python3
LLVM_CLI_BINARY ?= $(shell command -v llvm)

.PHONY: help check build macos-core macos-runtime macos-public-runtime macos-compiler-runtime macos-packer macos-bundles macos-sdk darwin-resources macos-final macos-status test audit
.DEFAULT_GOAL := help

help:
	@printf '%s\n' 'LLVM-CLI development commands:' \
	  '  make check  Verify native link inputs without changing files' \
	  '  make build  Repack and relink the macOS command' \
	  '  make macos-core  Build pinned LLVM in the 24 GiB workspace' \
	  '  make macos-runtime  Build the matching static C++ runtimes' \
	  '  make macos-public-runtime  Build native C++ archives for compiled programs' \
	  '  make macos-compiler-runtime  Build macOS compiler runtime libraries' \
	  '  make macos-packer  Build the static payload packer' \
	  '  make macos-bundles  Incrementally rebuild and compress tool modules' \
	  '  make macos-sdk  Stage matching SDK resources as a payload overlay' \
	  '  make darwin-resources ZIG_LIB_DIR=...  Reuse Darwin C headers and stubs' \
	  '  make macos-final  Assemble the complete candidate for verification' \
	  '  make macos-status  Show workspace usage and recent progress' \
	  '  make test   Test the existing command and release tooling' \
	  '  make audit  Check public source for private paths and credentials' \
	  'See docs/building.md for storage and build-input details.'

check:
	$(PYTHON) -B tools/build_final.py --check

build:
	$(PYTHON) -B tools/build_final.py

macos-core:
	$(PYTHON) -B tools/macos_build.py --build --jobs 4

macos-runtime:
	$(PYTHON) -B tools/macos_build.py --runtime --jobs 4

macos-public-runtime:
	$(PYTHON) -B tools/macos_build.py --public-runtime --jobs 2

macos-compiler-runtime:
	$(PYTHON) -B tools/macos_build.py --compiler-runtime --jobs 2

macos-packer:
	$(PYTHON) -B tools/macos_build.py --packer

macos-bundles:
	$(PYTHON) -B tools/build_native_bundles.py --jobs 4

macos-sdk:
	$(PYTHON) -B tools/prepare_native_payload.py --stage --jobs 2

darwin-resources:
	@test -n "$(ZIG_LIB_DIR)" || { printf '%s\n' 'Set ZIG_LIB_DIR to an existing Zig 0.17.0 library directory.'; exit 1; }
	$(PYTHON) -B tools/prepare_darwin_resources.py --zig-lib "$(ZIG_LIB_DIR)" --overlay build-work/macos-23.1.6/volume/overlay

macos-final:
	$(PYTHON) -B tools/macos_build.py --final

macos-status:
	$(PYTHON) -B tools/macos_build.py --status

test:
	@test -x "$(LLVM_CLI_BINARY)" || { printf '%s\n' 'Set LLVM_CLI_BINARY to an existing LLVM-CLI executable.' >&2; exit 1; }
	LLVM_CLI_BINARY="$(LLVM_CLI_BINARY)" $(PYTHON) -B -m unittest discover -s tests -p 'test_*.py' -v

audit:
	$(PYTHON) -B tools/audit_public.py
