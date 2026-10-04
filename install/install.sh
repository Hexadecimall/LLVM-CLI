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

is_newer_version() {
    printf '%s\n%s\n' "$1" "$2" | awk '
        function parse(value, parts, count, component) {
            sub(/^v/, "", value)
            count = split(value, parts, /\./)
            if (count != 3) return 0
            for (component = 1; component <= 3; component++)
                if (parts[component] !~ /^[0-9]+$/) return 0
            return 1
        }
        NR == 1 { installed = $0 }
        NR == 2 { available = $0 }
        END {
            if (!parse(installed, current) || !parse(available, release))
                exit 1
            for (i = 1; i <= 3; i++) {
                if (current[i] + 0 > release[i] + 0) exit 0
                if (current[i] + 0 < release[i] + 0) exit 1
            }
            exit 1
        }
    '
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

installed="$install_dir/llvm"
if [ "$force" -eq 0 ] && [ -f "$installed" ]; then
    installed_version=$("$installed" --version 2>/dev/null |
        sed -n '1s/^LLVM-CLI \([^ ]*\).*/\1/p')
    if is_newer_version "$installed_version" "$version"; then
        printf 'LLVM-CLI %s is newer than release %s; keeping %s (use --force to downgrade)\n' \
            "$installed_version" "$version" "$installed"
        exit 0
    fi
fi

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
case "$(sed -n '1p' "$manifest")" in
    llvm-cli-release-v1) compression=none ;;
    llvm-cli-release-v2) compression=gzip ;;
    *) fail "unsupported release manifest" ;;
esac

entry=$(awk -v p="$platform" '$1 == "binary" && $2 == p { print }' "$manifest")
set -- $entry
if [ "$compression" = gzip ]; then
    [ "$#" -eq 6 ] || fail "release $version has no native $platform binary"
    [ "$6" = gzip ] || fail "unsupported release compression"
    command -v gzip >/dev/null 2>&1 || fail "gzip is required for this release"
else
    [ "$#" -eq 5 ] || fail "release $version has no native $platform binary"
fi
check_number "$3"
check_hash "$4"
check_number "$5"
expected_size=$3
expected_hash=$4
part_count=$5
[ "$expected_size" -gt 0 ] && [ "$part_count" -gt 0 ] &&
    [ "$part_count" -le 999 ] || fail "invalid binary size or part count"

if [ "$force" -eq 0 ] && [ -f "$installed" ] &&
   { [ "$case_sensitive" -eq 0 ] ||
     { [ -L "$install_dir/LLVM" ] &&
       [ "$(readlink "$install_dir/LLVM")" = llvm ]; }; }; then
    installed_size=$(wc -c < "$installed" | tr -d ' ')
    if [ "$installed_size" -eq "$expected_size" ] &&
       [ "$(hash_file "$installed")" = "$expected_hash" ]; then
        printf 'LLVM-CLI %s is already installed in %s\n' "$version" "$install_dir"
        exit 0
    fi
fi

image="$work/llvm"
payload=$image
if [ "$compression" = gzip ]; then payload="$work/llvm.gz"; fi
: > "$payload"
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
    cat "$part" >> "$payload"
    rm -- "$part"
    index=$((index + 1))
done

if [ "$compression" = gzip ]; then
    gzip -dc "$payload" > "$image" || fail "gzip decompression failed"
    rm -- "$payload"
fi

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
