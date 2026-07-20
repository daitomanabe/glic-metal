#!/usr/bin/env bash

# Reproducible original-style Metal certification on explicit test assets.
# Video QA uses the global video-render-qa skill unless --skip-video-qa is set.

set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
normal_image=""
noise_image=""
video_input=""
output_dir=""
build_dir="${repo_root}/build-validation"
preset="vv02"
jobs="${GLIC_BUILD_JOBS:-10}"
skip_video_qa="0"
python_bin="${GLIC_VALIDATION_PYTHON:-}"

usage() {
  /bin/echo "Usage: $0 --normal-image PNG --noise-image PNG --video VIDEO --output-dir DIR [options]"
  /bin/echo "Options: --build-dir DIR --preset NAME --jobs N --skip-video-qa"
}

fail() {
  /bin/echo "error: $1" >&2
  exit 1
}

run_logged() {
  local label="$1"
  local log="$2"
  local status
  shift 2
  /bin/echo "[original-metal] ${label}"
  "$@" >"$log" 2>&1
  status="$?"
  if [ "$status" -ne 0 ]; then
    /usr/bin/tail -80 "$log" >&2
    fail "${label} failed with exit ${status}"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --normal-image) [ "$#" -ge 2 ] || fail "$1 needs a value"; normal_image="$2"; shift 2 ;;
    --noise-image) [ "$#" -ge 2 ] || fail "$1 needs a value"; noise_image="$2"; shift 2 ;;
    --video) [ "$#" -ge 2 ] || fail "$1 needs a value"; video_input="$2"; shift 2 ;;
    --output-dir) [ "$#" -ge 2 ] || fail "$1 needs a value"; output_dir="$2"; shift 2 ;;
    --build-dir) [ "$#" -ge 2 ] || fail "$1 needs a value"; build_dir="$2"; shift 2 ;;
    --preset) [ "$#" -ge 2 ] || fail "$1 needs a value"; preset="$2"; shift 2 ;;
    --jobs) [ "$#" -ge 2 ] || fail "$1 needs a value"; jobs="$2"; shift 2 ;;
    --skip-video-qa) skip_video_qa="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" ;;
  esac
done

for required in "$normal_image" "$noise_image" "$video_input"; do
  [ -f "$required" ] || fail "input does not exist: ${required:-<empty>}"
done
[ -n "$output_dir" ] || fail "--output-dir is required"
case "$jobs" in ''|*[!0-9]*) fail "--jobs must be a positive integer" ;; esac
[ "$jobs" -gt 0 ] || fail "--jobs must be a positive integer"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
if [ -z "$python_bin" ]; then
  python_bin="$(command -v python3)"
fi
[ -x "$python_bin" ] || fail "Python is not executable: $python_bin"
cmake_bin="$(command -v cmake)"
ctest_bin="$(command -v ctest)"
[ -n "$cmake_bin" ] || fail "cmake was not found"
[ -n "$ctest_bin" ] || fail "ctest was not found"
/bin/mkdir -p "$output_dir" "$build_dir" \
  "$output_dir/cpu-reference-previews" \
  "$output_dir/metal-normal-previews" \
  "$output_dir/metal-noise-previews" || fail "could not create output directories"

bench="$build_dir/glic_original_realtime_bench"
filter_bin="$build_dir/glic_original_visual_filter"
validator="$script_dir/validate_original_metal_run.py"
comparison="$script_dir/compare_original_metal_reference.py"
cpu_json="$output_dir/benchmark-cpu-reference.json"
normal_json="$output_dir/benchmark-metal-normal.json"
noise_json="$output_dir/benchmark-metal-noise.json"
comparison_json="$output_dir/reference-comparison.json"
comparison_md="$output_dir/reference-comparison.md"
video_output="$output_dir/${preset}-original-metal-960x540-30fps.mp4"
video_json="$output_dir/${preset}-original-metal-960x540-30fps.report.json"
control_output="$output_dir/passthrough-control-960x540-30fps.mp4"
control_json="$output_dir/passthrough-control-960x540-30fps.report.json"
effect_json="$output_dir/${preset}-original-metal.effect-difference.json"
effect_md="$output_dir/${preset}-original-metal.effect-difference.md"
effect_heatmap="$output_dir/${preset}-original-metal.effect-difference.png"
qa_json="$output_dir/${preset}-original-metal-960x540-30fps.qa.json"
qa_md="$output_dir/${preset}-original-metal-960x540-30fps.qa.md"

run_logged "configure Release build" "$output_dir/configure.log" \
  "$cmake_bin" -S "$repo_root" -B "$build_dir" -DCMAKE_BUILD_TYPE=Release
run_logged "build" "$output_dir/build.log" \
  "$cmake_bin" --build "$build_dir" --parallel "$jobs"
run_logged "CTest" "$output_dir/ctest.log" \
  "$ctest_bin" --test-dir "$build_dir" --output-on-failure

"$python_bin" -c 'import cv2, numpy' >/dev/null 2>&1 || \
  fail "validation Python requires cv2 and numpy: $python_bin"

/bin/echo "[original-metal] CPU reference previews"
"$bench" "$normal_image" --all-supported --presets-dir "$repo_root/presets" \
  --backend cpu --frames 120 --warmup 10 --require-fps 30 \
  --output-dir "$output_dir/cpu-reference-previews" --json "$cpu_json" \
  >"$output_dir/benchmark-cpu-reference.txt" 2>&1
cpu_status="$?"
if [ "$cpu_status" -gt 1 ]; then
  /usr/bin/tail -80 "$output_dir/benchmark-cpu-reference.txt" >&2
  fail "CPU reference generation failed with exit ${cpu_status}"
fi
run_logged "validate CPU reference" "$output_dir/validate-cpu.log" \
  "$python_bin" "$validator" --cpu-benchmark "$cpu_json"

run_logged "Metal normal benchmark" "$output_dir/benchmark-metal-normal.txt" \
  "$bench" "$normal_image" --all-supported --presets-dir "$repo_root/presets" \
  --backend metal --frames 120 --warmup 10 --require-fps 30 \
  --output-dir "$output_dir/metal-normal-previews" --json "$normal_json"
run_logged "validate Metal normal benchmark" "$output_dir/validate-metal-normal.log" \
  "$python_bin" "$validator" --metal-normal "$normal_json"

run_logged "CPU/Metal image comparison" "$output_dir/reference-comparison.log" \
  "$python_bin" "$comparison" \
  --cpu-dir "$output_dir/cpu-reference-previews" \
  --metal-dir "$output_dir/metal-normal-previews" \
  --cpu-benchmark "$cpu_json" --benchmark "$normal_json" \
  --output-json "$comparison_json" --output-md "$comparison_md"
run_logged "validate image comparison" "$output_dir/validate-comparison.log" \
  "$python_bin" "$validator" --comparison "$comparison_json"

run_logged "Metal noise benchmark" "$output_dir/benchmark-metal-noise.txt" \
  "$bench" "$noise_image" --all-supported --presets-dir "$repo_root/presets" \
  --backend metal --frames 120 --warmup 10 --require-fps 30 \
  --output-dir "$output_dir/metal-noise-previews" --json "$noise_json"
run_logged "validate Metal noise benchmark" "$output_dir/validate-metal-noise.log" \
  "$python_bin" "$validator" --metal-noise "$noise_json"

run_logged "process 960x540/30fps video" "$output_dir/process-video.log" \
  "$python_bin" "$script_dir/process_video.py" "$video_input" "$video_output" \
  --processing-mode original_visual --backend metal --preset "$preset" \
  --width 960 --height 540 --fps 30 --filter-bin "$filter_bin" \
  --report "$video_json" --overwrite
run_logged "validate video performance" "$output_dir/validate-video.log" \
  "$python_bin" "$validator" --video-report "$video_json"

run_logged "create passthrough codec control" "$output_dir/process-control.log" \
  "$python_bin" "$script_dir/process_video.py" "$video_input" "$control_output" \
  --passthrough --width 960 --height 540 --fps 30 \
  --filter-bin "$build_dir/glic_realtime_filter" \
  --report "$control_json" --overwrite
run_logged "measure visible dry/wet difference" "$output_dir/effect-difference.log" \
  "$python_bin" "$repo_root/tools/evaluate_effect_difference.py" "$video_input" \
  --control "$control_output" --candidate "${preset}=${video_output}" \
  --output-json "$effect_json" --output-md "$effect_md" \
  --heatmap "$effect_heatmap"
run_logged "validate visible dry/wet difference" "$output_dir/validate-effect-difference.log" \
  "$python_bin" "$validator" --effect-difference "$effect_json"

if [ "$skip_video_qa" -eq 0 ]; then
  skills_root="${AGENT_SKILLS_ROOT:-${HOME}/.codex/skills}"
  qa_guard="$skills_root/video-render-qa-python-env/scripts/ensure_video_render_qa_python.py"
  qa_evaluator="$skills_root/video-render-qa/scripts/evaluate_video_render.py"
  [ -f "$qa_guard" ] || fail "video-render-qa Python guard was not found: $qa_guard"
  [ -f "$qa_evaluator" ] || fail "video-render-qa evaluator was not found: $qa_evaluator"
  qa_python="$($python_bin "$qa_guard" --cwd "$repo_root" --print-python)"
  qa_status="$?"
  [ "$qa_status" -eq 0 ] || fail "video-render-qa Python guard failed"
  run_logged "technical video QA" "$output_dir/video-qa.log" \
    "$qa_python" "$qa_evaluator" "$video_output" \
    --output-json "$qa_json" --output-md "$qa_md"
  run_logged "validate technical video QA" "$output_dir/validate-video-qa.log" \
    "$python_bin" "$validator" --qa-report "$qa_json"
else
  /bin/echo "[original-metal] video QA skipped explicitly"
fi

if [ "$skip_video_qa" -eq 0 ]; then
  /bin/echo "PASS original Metal validation: $output_dir"
else
  /bin/echo "PASS remote stages; certification awaits technical video QA: $output_dir"
fi
