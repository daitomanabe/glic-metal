#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
output_dir="${1:-${repo_root}/build/codec-lab-tools}"
mkdir -p "${output_dir}"

if ! command -v pkg-config >/dev/null 2>&1; then
  echo "error: pkg-config is required" >&2
  exit 1
fi
if ! pkg-config --exists libavformat libavcodec libavutil; then
  echo "error: FFmpeg development packages are required" >&2
  exit 1
fi

cc -std=c11 -O2 -Wall -Wextra -Werror \
  "${repo_root}/tools/glic_extract_mvs.c" \
  $(pkg-config --cflags --libs libavformat libavcodec libavutil) \
  -o "${output_dir}/glic_extract_mvs"

echo "${output_dir}/glic_extract_mvs"
