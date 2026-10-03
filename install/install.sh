#!/bin/sh
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
set -eu
set -f

repo=https://github.com/Hexadecimall/LLVM-CLI
version=${LLVM_CLI_VERSION:-latest}
install_dir=${LLVM_CLI_INSTALL_DIR:-}
force=0
work=

fail() {
    printf 'LLVM-CLI installer: %s\n' "$*" >&2
    exit 1
}

usage() {
    printf 'Usage: sh install.sh [--version TAG] [--install-dir ABSOLUTE_PATH] [--force]\n'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version|--install-dir)
            [ "$#" -ge 2 ] || fail "missing value for $1"
            if [ "$1" = --version ]; then version=$2; else install_dir=$2; fi
            shift 2
            ;;
        --force) force=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
done

cleanup() {
    if [ -n "$work" ] && [ -d "$work" ]; then
        rm -r -- "$work"
    fi
}
trap cleanup 0
trap 'exit 1' 1 2 3 15

command -v curl >/dev/null 2>&1 || fail "curl is required"
if command -v sha256sum >/dev/null 2>&1; then
    hash_command=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hash_command=shasum
else
    fail "sha256sum or shasum is required"
fi

hash_file() {
    if [ "$hash_command" = shasum ]; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

check_hash() {
    [ "${#1}" -eq 64 ] || fail "invalid SHA-256 in release manifest"
    case "$1" in *[!0-9a-f]*) fail "invalid SHA-256 in release manifest" ;; esac
}

check_number() {
    case "$1" in ''|*[!0-9]*) fail "invalid number in release manifest" ;; esac
}

case "$(uname -s):$(uname -m)" in
    Darwin:arm64) platform=darwin-arm64 ;;
    Linux:x86_64) platform=linux-x86_64 ;;
    Linux:aarch64|Linux:arm64) platform=linux-aarch64 ;;
    *) fail "no native release for $(uname -s)/$(uname -m)" ;;
esac

if [ -z "$install_dir" ]; then
    if [ -w /usr/local/bin ]; then
        install_dir=/usr/local/bin
    else
        install_dir="$HOME/.local/bin"
    fi
fi
case "$install_dir" in /*) ;; *) fail "install directory must be an absolute path" ;; esac

case "$version" in
    latest)
        latest_url=$(curl -fsSL --proto '=https' --proto-redir '=https' \
            --connect-timeout 15 -o /dev/null -w '%{url_effective}' \
            "$repo/releases/latest") || fail "could not find the latest release"
        case "$latest_url" in
            "$repo/releases/tag/"*) version=${latest_url##*/} ;;
            *) fail "latest release did not resolve to a tag" ;;
        esac
        ;;
esac
case "$version" in ''|*[!A-Za-z0-9._-]*) fail "invalid release tag" ;; esac
base="$repo/releases/download/$version"

mkdir -p "$install_dir" || fail "cannot create $install_dir"
[ -w "$install_dir" ] || fail "cannot write to $install_dir; use --install-dir"

for name in LLVM llvm; do
    target="$install_dir/$name"
    if [ -d "$target" ]; then fail "$target is a directory"; fi
    if [ "$force" -eq 0 ] && { [ -e "$target" ] || [ -L "$target" ]; }; then
        if ! "$target" --version 2>/dev/null | grep -q '^LLVM-CLI '; then
            fail "$target exists and is not LLVM-CLI; use --force to replace it"
        fi
    fi
done

work=$(mktemp -d "$install_dir/.llvm-cli-install.XXXXXXXX") ||
    fail "could not create an installation staging directory"
touch "$work/case-test"
if [ -e "$work/CASE-TEST" ]; then
    case_sensitive=0
else
    case_sensitive=1
fi
rm -- "$work/case-test"
manifest="$work/llvm-cli-manifest-v1.txt"
curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 \
    --connect-timeout 15 -o "$manifest" "$base/llvm-cli-manifest-v1.txt" ||
    fail "release $version has no installer manifest"
[ "$(sed -n '1p' "$manifest")" = llvm-cli-release-v1 ] ||
    fail "unsupported release manifest"

entry=$(awk -v p="$platform" '$1 == "binary" && $2 == p { print }' "$manifest")
set -- $entry
[ "$#" -eq 5 ] || fail "release $version has no native $platform binary"
check_number "$3"
check_hash "$4"
check_number "$5"
expected_size=$3
expected_hash=$4
part_count=$5
[ "$expected_size" -gt 0 ] && [ "$part_count" -gt 0 ] &&
    [ "$part_count" -le 999 ] || fail "invalid binary size or part count"

image="$work/llvm"
: > "$image"
index=0
while [ "$index" -lt "$part_count" ]; do
    part_entry=$(awk -v p="$platform" -v n="$index" \
        '$1 == "part" && $2 == p && $3 == n { print }' "$manifest")
    set -- $part_entry
    [ "$#" -eq 5 ] || fail "missing or duplicate part $index"
    check_number "$4"
    check_hash "$5"
    [ "$4" -gt 0 ] && [ "$4" -le 1073741824 ] ||
        fail "invalid size for part $index"
    part_size=$4
    part_hash=$5
    part_name=$(printf 'llvm-cli-%s.part%03d' "$platform" "$index")
    part="$work/$part_name"
    curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 \
        --connect-timeout 15 -o "$part" "$base/$part_name" ||
        fail "failed to download $part_name"
    actual_size=$(wc -c < "$part" | tr -d ' ')
    [ "$actual_size" -eq "$part_size" ] || fail "size mismatch: $part_name"
    [ "$(hash_file "$part")" = "$part_hash" ] ||
        fail "SHA-256 mismatch: $part_name"
    cat "$part" >> "$image"
    rm -- "$part"
    index=$((index + 1))
done

actual_size=$(wc -c < "$image" | tr -d ' ')
[ "$actual_size" -eq "$expected_size" ] || fail "assembled binary size mismatch"
[ "$(hash_file "$image")" = "$expected_hash" ] ||
    fail "assembled binary SHA-256 mismatch"
chmod 755 "$image"

if [ "$platform" = darwin-arm64 ]; then
    codesign --verify --strict "$image" >/dev/null 2>&1 ||
        fail "macOS code-signature verification failed"
fi
"$image" --version >/dev/null || fail "downloaded LLVM-CLI did not start"

mv -f "$image" "$install_dir/llvm"
if [ "$case_sensitive" -eq 1 ]; then
    ln -s llvm "$work/LLVM"
    mv -f "$work/LLVM" "$install_dir/LLVM"
fi
printf 'Installed LLVM-CLI %s for %s in %s\n' "$version" "$platform" "$install_dir"
case ":$PATH:" in
    *":$install_dir:"*) ;;
    *) printf 'Add %s to PATH to use the llvm command.\n' "$install_dir" >&2 ;;
esac
