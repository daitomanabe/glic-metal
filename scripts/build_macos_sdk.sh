#!/usr/bin/env bash

# Build a drop-in macOS SDK: XCFramework + runtime resource bundle.

set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
output_dir="${1:-${repo_root}/build/GlicMetalSDK}"
build_dir="${GLIC_SDK_BUILD_DIR:-${repo_root}/build-sdk}"
architectures="${GLIC_SDK_ARCHITECTURES:-$(uname -m)}"
developer_dir="${GLIC_XCODE_DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"

fail() {
  /bin/echo "error: $1" >&2
  exit 1
}

run() {
  "$@"
  status="$?"
  [ "$status" -eq 0 ] || fail "command failed with exit ${status}: $*"
}

[ "$(uname -s)" = "Darwin" ] || fail "the SDK builder requires macOS"
[ -x "${developer_dir}/usr/bin/xcodebuild" ] ||
  fail "full Xcode was not found at ${developer_dir}"
[ ! -e "$output_dir" ] ||
  fail "output already exists; choose a new path: ${output_dir}"

temporary_root="$(mktemp -d)" || fail "could not create temporary directory"
cleanup() {
  if [ -n "${temporary_root:-}" ] && [ -d "$temporary_root" ]; then
    /bin/rm -rf "$temporary_root"
  fi
}
trap cleanup EXIT INT TERM

install_dir="${temporary_root}/install"
sdk_dir="${temporary_root}/GlicMetalSDK"
resource_bundle="${sdk_dir}/GlicMetalResources.bundle"

run cmake -S "$repo_root" -B "$build_dir" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="$architectures" \
  -DGLIC_BUILD_STANDALONE=OFF \
  -DGLIC_INSTALL=ON
run cmake --build "$build_dir" --target glic_core --parallel
run cmake --install "$build_dir" --prefix "$install_dir"

run /bin/mkdir -p "$sdk_dir"
run env "DEVELOPER_DIR=${developer_dir}" /usr/bin/xcodebuild \
  -create-xcframework \
  -library "${install_dir}/lib/libglic_metal.a" \
  -headers "${install_dir}/include" \
  -output "${sdk_dir}/GlicMetal.xcframework"

run /bin/mkdir -p "${resource_bundle}/Contents/Resources"
run cmake -E copy_directory \
  "${install_dir}/share/glic-metal/presets" \
  "${resource_bundle}/Contents/Resources/Presets"
run cmake -E copy_if_different \
  "${install_dir}/lib/glic/glic_realtime.metallib" \
  "${resource_bundle}/Contents/Resources/glic_realtime.metallib"
run cmake -E copy_if_different "$repo_root/LICENSE" \
  "${resource_bundle}/Contents/Resources/LICENSE"
run cmake -E copy_if_different "$repo_root/THIRD_PARTY_NOTICES.md" \
  "${resource_bundle}/Contents/Resources/THIRD_PARTY_NOTICES.md"
run cmake -E copy_if_different "$repo_root/resources/SDK-README.md" \
  "${sdk_dir}/README.md"

info_plist="${resource_bundle}/Contents/Info.plist"
run /usr/bin/plutil -create xml1 "$info_plist"
run /usr/bin/plutil -insert CFBundleIdentifier -string \
  ws.daito.glic-metal.resources "$info_plist"
run /usr/bin/plutil -insert CFBundleName -string GlicMetalResources \
  "$info_plist"
run /usr/bin/plutil -insert CFBundlePackageType -string BNDL "$info_plist"
run /usr/bin/plutil -insert CFBundleShortVersionString -string 1.0.0 \
  "$info_plist"
run /usr/bin/plutil -insert CFBundleVersion -string 1 "$info_plist"

(
  cd "$sdk_dir" || exit 1
  find GlicMetal.xcframework GlicMetalResources.bundle -type f -print0 |
    sort -z | xargs -0 /usr/bin/shasum -a 256 > SHA256SUMS
) || fail "could not create SDK checksums"

run /bin/mkdir -p "$(dirname "$output_dir")"
run /bin/mv "$sdk_dir" "$output_dir"
/bin/echo "GLIC Metal SDK: ${output_dir}"
