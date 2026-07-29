#!/usr/bin/env bash

# Build a drop-in macOS SDK: XCFramework + runtime resource bundle.

set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
output_dir="${1:-${repo_root}/build/GlicMetalSDK}"
build_dir="${GLIC_SDK_BUILD_DIR:-${repo_root}/build-sdk}"
architectures="${GLIC_SDK_ARCHITECTURES:-$(uname -m)}"
developer_dir="${GLIC_XCODE_DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
sdk_version="${GLIC_SDK_VERSION:-0.1.0-dev}"
deployment_target="${GLIC_SDK_DEPLOYMENT_TARGET:-13.0}"
source_repository="${GLIC_SDK_SOURCE_REPOSITORY:-https://github.com/daitomanabe/glic-metal}"
source_revision="${GLIC_SDK_SOURCE_REVISION:-$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)}"
build_number="${GLIC_SDK_BUILD_NUMBER:-$(git -C "$repo_root" rev-list --count HEAD 2>/dev/null || echo 1)}"
require_clean="${GLIC_SDK_REQUIRE_CLEAN:-0}"

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
[ -n "$source_revision" ] || fail "could not determine the source Git revision"

source_dirty=0
git -C "$repo_root" diff --quiet --ignore-submodules -- || source_dirty=1
git -C "$repo_root" diff --cached --quiet --ignore-submodules -- ||
  source_dirty=1
if [ -n "$(git -C "$repo_root" ls-files --others --exclude-standard)" ]; then
  source_dirty=1
fi
if [ "$require_clean" = "1" ] && [ "$source_dirty" != "0" ]; then
  fail "GLIC_SDK_REQUIRE_CLEAN=1 but the source worktree is dirty"
fi

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
tools_dir="${sdk_dir}/Tools"
documentation_dir="${sdk_dir}/Documentation"
skills_dir="${sdk_dir}/Skills"

run cmake -S "$repo_root" -B "$build_dir" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="$architectures" \
  -DCMAKE_OSX_DEPLOYMENT_TARGET="$deployment_target" \
  -DGLIC_BUILD_STANDALONE=OFF \
  -DGLIC_INSTALL=ON
run cmake --build "$build_dir" --target glic_core glic_codec_glitch_filter \
  --parallel
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
  "${install_dir}/share/glic-metal/selected-presets.json" \
  "${resource_bundle}/Contents/Resources/selected-presets.json"
run cmake -E copy_if_different \
  "${install_dir}/share/glic-metal/integration-manifest.json" \
  "${resource_bundle}/Contents/Resources/integration-manifest.json"
run cmake -E copy_if_different \
  "${install_dir}/share/glic-metal/offline-codec-effects.json" \
  "${resource_bundle}/Contents/Resources/offline-codec-effects.json"
run cmake -E copy_if_different \
  "${install_dir}/share/glic-metal/codec-lab-effects.json" \
  "${resource_bundle}/Contents/Resources/codec-lab-effects.json"
run cmake -E copy_if_different \
  "${install_dir}/share/glic-metal/glitch-gallery-presets.json" \
  "${resource_bundle}/Contents/Resources/glitch-gallery-presets.json"
run cmake -E copy_if_different \
  "${install_dir}/lib/glic/glic_realtime.metallib" \
  "${resource_bundle}/Contents/Resources/glic_realtime.metallib"
run cmake -E copy_if_different "$repo_root/LICENSE" \
  "${resource_bundle}/Contents/Resources/LICENSE"
run cmake -E copy_if_different "$repo_root/THIRD_PARTY_NOTICES.md" \
  "${resource_bundle}/Contents/Resources/THIRD_PARTY_NOTICES.md"
run cmake -E copy_if_different "$repo_root/resources/SDK-README.md" \
  "${sdk_dir}/README.md"
run cmake -E copy_if_different \
  "${install_dir}/share/doc/glic-metal/AI_INTEGRATION.md" \
  "${sdk_dir}/AI_INTEGRATION.md"

release_manifest="${sdk_dir}/RELEASE-MANIFEST.json"
SDK_VERSION="$sdk_version" \
SOURCE_REPOSITORY="$source_repository" \
SOURCE_REVISION="$source_revision" \
SOURCE_DIRTY="$source_dirty" \
BUILD_NUMBER="$build_number" \
ARCHITECTURES="$architectures" \
DEPLOYMENT_TARGET="$deployment_target" \
python3 - "$release_manifest" <<'PY'
import json
import os
from pathlib import Path
import sys

manifest = {
    "schema": "glic-metal-sdk-release-v1",
    "sdk_version": os.environ["SDK_VERSION"],
    "source_repository": os.environ["SOURCE_REPOSITORY"],
    "source_revision": os.environ["SOURCE_REVISION"],
    "source_dirty": os.environ["SOURCE_DIRTY"] != "0",
    "build_number": int(os.environ["BUILD_NUMBER"]),
    "platform": "macOS",
    "architectures": [
        value
        for value in os.environ["ARCHITECTURES"].replace(",", ";").split(";")
        if value
    ],
    "minimum_macos": os.environ["DEPLOYMENT_TARGET"],
    "abi_versions": {
        "image": 1,
        "codec": 1,
        "selected_presets": 1,
    },
    "production_presets": {
        "count": 28,
        "category_counts": {
            "original": 14,
            "spatial": 8,
            "codec": 6,
        },
    },
}
Path(sys.argv[1]).write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
[ "$?" -eq 0 ] || fail "could not create SDK release manifest"
run cmake -E copy_if_different \
  "$release_manifest" \
  "${resource_bundle}/Contents/Resources/RELEASE-MANIFEST.json"

run /bin/mkdir -p "$tools_dir" "$documentation_dir" "$skills_dir"
run cmake -E copy_directory \
  "${repo_root}/skills/glic-metal-sdk-integration" \
  "${skills_dir}/glic-metal-sdk-integration"
run /usr/bin/find "$skills_dir" -type d -name __pycache__ -prune \
  -exec /bin/rm -rf {} +
run cmake -E copy_if_different \
  "${install_dir}/bin/glic_codec_glitch_filter" \
  "${tools_dir}/glic_codec_glitch_filter"
run cmake -E copy_if_different \
  "${install_dir}/lib/glic/glic_realtime.metallib" \
  "${tools_dir}/glic_realtime.metallib"
run cmake -E copy_if_different \
  "${install_dir}/bin/glic_process_video" \
  "${tools_dir}/process_video.py"
for tool in \
  process_multicodec_glitch.py \
  process_offline_packet_glitch.py \
  evaluate_offline_packet_glitches.py \
  process_codec_lab.py \
  process_native_syntax_glitch.py \
  native_syntax_glitch.py \
  x265_analysis_glitch.py \
  evaluate_native_syntax_glitches.py \
  process_structured_codec_glitch.py \
  structured_bitstream.py \
  process_transport_glitch.py \
  transport_glitch.py \
  process_metadata_glitch.py \
  evolutionary_codec_search.py \
  probe_multicodec_capabilities.py \
  build_av2_reference.py \
  build_vvc_reference.py \
  build_ffmpeg_hevc_glitch_reference.py \
  build_x264_glitch_reference.py \
  build_x265_glitch_reference.py \
  ffmpeg-8.0.1-hevc-glic-decoder-hooks.patch \
  ffmpeg_hevc_glic_decoder_hook.h \
  ffmpeg_hevc_glic_decoder_hook.c \
  x264-0480cb0-glic-entropy-hooks.patch \
  x264_glic_entropy_hook.h \
  x264_glic_entropy_hook.c \
  x265-4.2-glic-entropy-hooks.patch \
  x265_glic_entropy_hook.h \
  install_ffglitch_reference.py \
  validate_videotoolbox_fast_path.py \
  evaluate_codec_glitch_videos.py \
  evaluate_effect_difference.py; do
  run cmake -E copy_if_different \
    "${install_dir}/bin/${tool}" \
    "${tools_dir}/${tool}"
done
run cmake -E copy_if_different \
  "${install_dir}/share/glic-metal/requirements-qa.txt" \
  "${tools_dir}/requirements.txt"
for document in \
  DOWNSTREAM_QUICKSTART.md \
  EMBEDDING.md \
  AI_INTEGRATION.md \
  CODEC_GLITCH.md \
  VIDEOTOOLBOX_FAST_PATH.md \
  MULTICODEC_GLITCH.md \
  OFFLINE_PACKET_GLITCH.md \
  CODEC_LAB.md \
  NATIVE_SYNTAX_GLITCH.md \
  GLITCH_ALGORITHM_GALLERY.md \
  GLITCH_EXPANSION.md; do
  run cmake -E copy_if_different \
    "${install_dir}/share/doc/glic-metal/${document}" \
    "${documentation_dir}/${document}"
done

info_plist="${resource_bundle}/Contents/Info.plist"
run /usr/bin/plutil -create xml1 "$info_plist"
run /usr/bin/plutil -insert CFBundleIdentifier -string \
  ws.daito.glic-metal.resources "$info_plist"
run /usr/bin/plutil -insert CFBundleName -string GlicMetalResources \
  "$info_plist"
run /usr/bin/plutil -insert CFBundlePackageType -string BNDL "$info_plist"
run /usr/bin/plutil -insert CFBundleShortVersionString -string \
  "${sdk_version%%-*}" \
  "$info_plist"
run /usr/bin/plutil -insert CFBundleVersion -string "$build_number" \
  "$info_plist"

(
  cd "$sdk_dir" || exit 1
  find GlicMetal.xcframework GlicMetalResources.bundle README.md \
    AI_INTEGRATION.md RELEASE-MANIFEST.json Documentation Tools Skills \
    -type f -print0 |
    sort -z | xargs -0 /usr/bin/shasum -a 256 > SHA256SUMS
) || fail "could not create SDK checksums"

run /bin/mkdir -p "$(dirname "$output_dir")"
run /bin/mv "$sdk_dir" "$output_dir"
/bin/echo "GLIC Metal SDK: ${output_dir}"
