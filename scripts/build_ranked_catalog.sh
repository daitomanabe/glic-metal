#!/usr/bin/env bash

# Build the complete offline ranking pipeline for one search directory.
# No network service or LLM is used; visual-liveliness stays an external,
# self-tested measurement instrument.

set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run_dir="${1:-}"
python_bin="${RANKING_PYTHON:-/usr/bin/python3}"
liveliness_runner="${VISUAL_LIVELINESS_RUNNER:-${HOME}/.codex/skills/visual-liveliness/scripts/run.sh}"

if [ -z "$run_dir" ]; then
  /bin/echo "Usage: $0 SEARCH_RUN_DIR" >&2
  exit 2
fi
if [ ! -d "$run_dir" ]; then
  /bin/echo "Search run directory does not exist: ${run_dir}" >&2
  exit 2
fi
if [ ! -x "$python_bin" ]; then
  /bin/echo "Ranking Python is not executable: ${python_bin}" >&2
  exit 2
fi
if [ ! -f "$liveliness_runner" ]; then
  /bin/echo "visual-liveliness runner was not found: ${liveliness_runner}" >&2
  exit 2
fi

run_dir="$(cd "$run_dir" && pwd)"
lock_path="${run_dir}/.ranking-pipeline.lock"
lock_owner_path="${lock_path}/pid"
stage_pid=""

cleanup() {
  if [ -d "$lock_path" ]; then
    owner="$(/bin/cat "$lock_owner_path" 2>/dev/null)"
    if [ "$owner" = "$$" ]; then
      /bin/rm -f "$lock_owner_path"
      /bin/rmdir "$lock_path" 2>/dev/null
    fi
  fi
}
handle_int() {
  if [ -n "$stage_pid" ] && /bin/kill -0 "$stage_pid" 2>/dev/null; then
    /bin/kill -TERM "$stage_pid" 2>/dev/null
    wait "$stage_pid" 2>/dev/null
  fi
  exit 130
}
handle_term() {
  if [ -n "$stage_pid" ] && /bin/kill -0 "$stage_pid" 2>/dev/null; then
    /bin/kill -TERM "$stage_pid" 2>/dev/null
    wait "$stage_pid" 2>/dev/null
  fi
  exit 143
}
run_stage() {
  "$@" &
  stage_pid="$!"
  wait "$stage_pid"
  status="$?"
  stage_pid=""
  return "$status"
}
trap cleanup EXIT
trap handle_int INT
trap handle_term TERM

if ! /bin/mkdir "$lock_path" 2>/dev/null; then
  existing_pid="$(/bin/cat "$lock_owner_path" 2>/dev/null)"
  case "$existing_pid" in
    ''|*[!0-9]*) existing_pid="" ;;
  esac
  if [ -n "$existing_pid" ] && /bin/kill -0 "$existing_pid" 2>/dev/null; then
    /bin/echo "Another ranking pipeline is active for ${run_dir} (PID ${existing_pid})" >&2
    exit 4
  fi
  /bin/rm -f "$lock_owner_path"
  /bin/rmdir "$lock_path" 2>/dev/null
  if ! /bin/mkdir "$lock_path" 2>/dev/null; then
    /bin/echo "Could not acquire ranking lock: ${lock_path}" >&2
    exit 4
  fi
fi
/usr/bin/printf '%s\n' "$$" > "$lock_owner_path"

if ! run_stage "$python_bin" "$script_dir/analyze_search_images.py" "$run_dir" \
    --runner "$liveliness_runner"; then
  /bin/echo "Image analysis failed; no partial ranking was published" >&2
  exit 1
fi
if ! run_stage "$python_bin" "$script_dir/build_search_catalog.py" "$run_dir" \
    --archive "$run_dir/ranking-archive.json"; then
  /bin/echo "Base catalog build failed" >&2
  exit 1
fi
if ! run_stage "$python_bin" "$script_dir/rank_search_results.py" "$run_dir"; then
  /bin/echo "Ranking build failed" >&2
  exit 1
fi

/bin/echo "Ranked catalog: ${run_dir}/ranking.html"
