#!/usr/bin/env bash

# Run the local Metal preset search without network services or LLM credentials.
#
# Exit codes:
#   0   search completed normally
#   2   invalid configuration
#   3   insufficient disk space before launch
#   4   another supervisor is already active for this output directory
#   75  search stopped because free disk space crossed the safety threshold
#   124 search exceeded its duration plus shutdown grace period
#   130 interrupted with SIGINT
#   143 interrupted with SIGTERM
#   other values are propagated from glic_realtime_search

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

search_bin="${SEARCH_BIN:-${repo_root}/build/glic_realtime_search}"
output_dir="${SEARCH_OUTPUT_DIR:-${repo_root}/search-runs/unattended}"
duration_seconds="${SEARCH_DURATION_SECONDS:-18000}"
max_candidates="${SEARCH_MAX_CANDIDATES:-0}"
search_seed="${SEARCH_SEED:-1337}"
search_backend="${SEARCH_BACKEND:-metal}"
min_free_gib="${MIN_FREE_GIB:-45}"
disk_check_seconds="${SEARCH_DISK_CHECK_SECONDS:-30}"
shutdown_grace_seconds="${SEARCH_SHUTDOWN_GRACE_SECONDS:-120}"
resume_requested="${SEARCH_RESUME:-0}"

status_path="${SEARCH_STATUS_PATH:-${output_dir}/supervisor-status.json}"
logs_dir="${output_dir}/logs"
supervisor_log="${logs_dir}/supervisor.log"
search_log="${logs_dir}/search.log"
catalog_log="${logs_dir}/catalog.log"
catalog_builder="${SEARCH_CATALOG_BUILDER:-${repo_root}/scripts/build_search_catalog.py}"
lock_path="${output_dir}/.search-supervisor.lock"
lock_owner_path="${lock_path}/pid"
search_pid_path="${output_dir}/.search.pid"

search_pid=""
runner_pid=""
started_epoch=""
deadline_epoch=""
status_state="initializing"
status_reason=""
status_exit_code=""
signal_exit_code=""
stop_reason=""
stop_started_epoch=""
free_kib="0"

usage() {
  /bin/echo "Usage: SEARCH_INPUT_ARGS=\"--input /path/a.png --input /path/b.png\" $0 [--resume] [search input arguments...]"
  /bin/echo ""
  /bin/echo "Configuration is supplied through SEARCH_BIN, SEARCH_OUTPUT_DIR,"
  /bin/echo "SEARCH_DURATION_SECONDS (default 18000), SEARCH_MAX_CANDIDATES,"
  /bin/echo "SEARCH_SEED, SEARCH_BACKEND, SEARCH_INPUT_ARGS, and MIN_FREE_GIB."
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

is_true() {
  case "$1" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

iso_now() {
  /bin/date -u +"%Y-%m-%dT%H:%M:%SZ"
}

read_free_kib() {
  local value
  value="$(/bin/df -Pk "$output_dir" 2>/dev/null | /usr/bin/awk 'NR == 2 { print $4 }')"
  if ! is_uint "$value"; then
    value="0"
  fi
  free_kib="$value"
}

write_status() {
  local exit_value="null"
  local resume_value="false"
  local free_bytes=$((free_kib * 1024))
  local min_free_bytes=$((min_free_gib * 1024 * 1024 * 1024))

  if is_uint "$status_exit_code"; then
    exit_value="$status_exit_code"
  fi
  if is_true "$resume_requested"; then
    resume_value="true"
  fi

  STATUS_STATE="$status_state" \
  STATUS_REASON="$status_reason" \
  STATUS_EXIT_CODE="$exit_value" \
  STATUS_RESUME="$resume_value" \
  STATUS_STARTED_EPOCH="${started_epoch:-0}" \
  STATUS_DEADLINE_EPOCH="${deadline_epoch:-0}" \
  STATUS_DURATION="$duration_seconds" \
  STATUS_PID="${search_pid:-0}" \
  STATUS_RUNNER_PID="${runner_pid:-0}" \
  STATUS_FREE_BYTES="$free_bytes" \
  STATUS_MIN_FREE_BYTES="$min_free_bytes" \
  STATUS_RUN_DIR="$output_dir" \
  STATUS_BINARY="$search_bin" \
  STATUS_SEARCH_LOG="$search_log" \
  /usr/bin/python3 - "$status_path" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone


def integer(name: str) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return 0


target = os.path.abspath(sys.argv[1])
os.makedirs(os.path.dirname(target), exist_ok=True)
exit_text = os.environ.get("STATUS_EXIT_CODE", "null")
payload = {
    "schema_version": 1,
    "state": os.environ.get("STATUS_STATE", "unknown"),
    "reason": os.environ.get("STATUS_REASON", ""),
    "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "started_epoch": integer("STATUS_STARTED_EPOCH"),
    "deadline_epoch": integer("STATUS_DEADLINE_EPOCH"),
    "duration_seconds": integer("STATUS_DURATION"),
    "pid": integer("STATUS_PID"),
    "runner_pid": integer("STATUS_RUNNER_PID"),
    "exit_code": None if exit_text == "null" else int(exit_text),
    "resume": os.environ.get("STATUS_RESUME") == "true",
    "free_bytes": integer("STATUS_FREE_BYTES"),
    "minimum_free_bytes": integer("STATUS_MIN_FREE_BYTES"),
    "output_dir": os.path.abspath(os.environ.get("STATUS_RUN_DIR", ".")),
    "binary": os.path.abspath(os.environ.get("STATUS_BINARY", "")),
    "search_log": os.path.abspath(os.environ.get("STATUS_SEARCH_LOG", "")),
}
fd, temporary = tempfile.mkstemp(prefix=".supervisor-status-", suffix=".json", dir=os.path.dirname(target))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

log_message() {
  local message="$1"
  /usr/bin/printf '%s %s\n' "$(iso_now)" "$message" | /usr/bin/tee -a "$supervisor_log"
}

request_search_stop() {
  local reason="$1"
  if [ -n "$stop_reason" ]; then
    return
  fi
  stop_reason="$reason"
  stop_started_epoch="$(/bin/date +%s)"
  status_state="stopping"
  status_reason="$reason"
  write_status
  log_message "requesting graceful search stop: ${reason}"
  if [ -n "$search_pid" ] && /bin/kill -0 "$search_pid" 2>/dev/null; then
    /bin/kill -INT "$search_pid" 2>/dev/null
  fi
}

handle_int() {
  signal_exit_code="130"
  request_search_stop "received_sigint"
}

handle_term() {
  signal_exit_code="143"
  request_search_stop "received_sigterm"
}

cleanup() {
  if [ -d "$lock_path" ]; then
    local owner
    owner="$(/bin/cat "$lock_owner_path" 2>/dev/null)"
    if [ "$owner" = "$$" ]; then
      /bin/rm -f "$lock_owner_path"
      /bin/rmdir "$lock_path" 2>/dev/null
    fi
  fi
  if [ -n "$search_pid" ] && ! /bin/kill -0 "$search_pid" 2>/dev/null; then
    /bin/rm -f "$search_pid_path"
  fi
}

trap handle_int INT
trap handle_term TERM
trap cleanup EXIT

cli_args=()
for argument in "$@"; do
  case "$argument" in
    --resume)
      resume_requested="1"
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      cli_args+=("$argument")
      ;;
  esac
done

if ! is_uint "$duration_seconds" || [ "$duration_seconds" -eq 0 ]; then
  /bin/echo "SEARCH_DURATION_SECONDS must be a positive integer" >&2
  exit 2
fi
if ! is_uint "$max_candidates" || ! is_uint "$search_seed"; then
  /bin/echo "SEARCH_MAX_CANDIDATES and SEARCH_SEED must be unsigned integers" >&2
  exit 2
fi
if ! is_uint "$min_free_gib" || ! is_uint "$disk_check_seconds" || [ "$disk_check_seconds" -eq 0 ]; then
  /bin/echo "MIN_FREE_GIB and SEARCH_DISK_CHECK_SECONDS must be positive integer values" >&2
  exit 2
fi
if ! is_uint "$shutdown_grace_seconds"; then
  /bin/echo "SEARCH_SHUTDOWN_GRACE_SECONDS must be an unsigned integer" >&2
  exit 2
fi
case "$search_backend" in
  metal|cpu|auto) ;;
  *)
    /bin/echo "SEARCH_BACKEND must be metal, cpu, or auto" >&2
    exit 2
    ;;
esac

if [ ! -x "$search_bin" ]; then
  /bin/echo "Search binary is not executable: ${search_bin}" >&2
  exit 2
fi
if [ ! -x /usr/bin/caffeinate ]; then
  /bin/echo "caffeinate is required and was not found at /usr/bin/caffeinate" >&2
  exit 2
fi

/bin/mkdir -p "$logs_dir"

if ! /bin/mkdir "$lock_path" 2>/dev/null; then
  existing_pid="$(/bin/cat "$lock_owner_path" 2>/dev/null)"
  if is_uint "$existing_pid" && /bin/kill -0 "$existing_pid" 2>/dev/null; then
    /bin/echo "A search supervisor is already running for ${output_dir} (PID ${existing_pid})" >&2
    exit 4
  fi
  /bin/rm -f "$lock_owner_path"
  /bin/rmdir "$lock_path" 2>/dev/null
  if ! /bin/mkdir "$lock_path" 2>/dev/null; then
    /bin/echo "Could not acquire supervisor lock for ${output_dir}" >&2
    exit 4
  fi
fi
/usr/bin/printf '%s\n' "$$" > "$lock_owner_path"

input_args=()
if [ -n "${SEARCH_INPUT_ARGS:-}" ]; then
  parsed_args_file="$(/usr/bin/mktemp -t glic-search-args.XXXXXX)"
  if ! SEARCH_INPUT_ARGS="$SEARCH_INPUT_ARGS" /usr/bin/python3 - > "$parsed_args_file" <<'PY'
import os
import shlex
import sys

try:
    arguments = shlex.split(os.environ.get("SEARCH_INPUT_ARGS", ""))
except ValueError as error:
    print(f"Invalid SEARCH_INPUT_ARGS: {error}", file=sys.stderr)
    raise SystemExit(2)
for argument in arguments:
    if "\n" in argument or "\r" in argument:
        print("SEARCH_INPUT_ARGS cannot contain newlines within an argument", file=sys.stderr)
        raise SystemExit(2)
    print(argument)
PY
  then
    /bin/rm -f "$parsed_args_file"
    exit 2
  fi
  while IFS= read -r argument; do
    input_args+=("$argument")
  done < "$parsed_args_file"
  /bin/rm -f "$parsed_args_file"
fi
for argument in "${cli_args[@]}"; do
  input_args+=("$argument")
done

has_input="0"
for argument in "${input_args[@]}"; do
  if [ "$argument" = "--input" ]; then
    has_input="1"
    break
  fi
done
if [ "$has_input" != "1" ]; then
  /bin/echo "At least one --input path is required in SEARCH_INPUT_ARGS or command-line arguments" >&2
  exit 2
fi

read_free_kib
minimum_free_kib=$((min_free_gib * 1024 * 1024))
if [ "$free_kib" -lt "$minimum_free_kib" ]; then
  status_state="refused_low_disk"
  status_reason="free_disk_below_initial_threshold"
  status_exit_code="3"
  write_status
  /bin/echo "Insufficient disk space: $((free_kib / 1024 / 1024)) GiB free, ${min_free_gib} GiB required" >&2
  exit 3
fi

command=(
  "$search_bin"
  "${input_args[@]}"
  --output-dir "$output_dir"
  --duration-seconds "$duration_seconds"
  --max-candidates "$max_candidates"
  --seed "$search_seed"
  --backend "$search_backend"
)
if is_true "$resume_requested"; then
  command+=(--resume)
fi

started_epoch="$(/bin/date +%s)"
deadline_epoch=$((started_epoch + duration_seconds))
status_state="starting"
status_reason=""
write_status

log_message "starting unattended search; duration=${duration_seconds}s output=${output_dir}"
log_message "LLM and hosted-generation API environment variables are removed from the search process"
/usr/bin/python3 - "$supervisor_log" "$(iso_now)" "${command[@]}" <<'PY'
import json
import sys

with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{sys.argv[2]} command_json: {json.dumps(sys.argv[3:])}\n")
PY

/bin/rm -f "$search_pid_path"
(
  clean_path="${PATH:-/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
  clean_home="${HOME:-/tmp}"
  clean_tmp="${TMPDIR:-/tmp}"
  clean_lang="${LANG:-C}"
  exec /usr/bin/env -i \
    HOME="$clean_home" PATH="$clean_path" TMPDIR="$clean_tmp" LANG="$clean_lang" \
    /bin/bash -c '
    pid_path="$1"
    shift
    printf "%s\n" "$$" > "$pid_path"
    /usr/bin/caffeinate -dimsu -w "$$" >/dev/null 2>&1 &
    exec "$@"
  ' glic-search-runner "$search_pid_path" "${command[@]}"
) >> "$search_log" 2>&1 &
runner_pid="$!"

pid_wait_count="0"
while [ ! -s "$search_pid_path" ] && /bin/kill -0 "$runner_pid" 2>/dev/null && [ "$pid_wait_count" -lt 100 ]; do
  /bin/sleep 0.1
  pid_wait_count=$((pid_wait_count + 1))
done
if [ -s "$search_pid_path" ]; then
  search_pid="$(/bin/cat "$search_pid_path" 2>/dev/null)"
fi
if ! is_uint "$search_pid"; then
  wait "$runner_pid"
  launch_exit="$?"
  status_state="failed"
  status_reason="search_process_did_not_start"
  status_exit_code="$launch_exit"
  write_status
  log_message "search process did not start; exit=${launch_exit}"
  if [ "$launch_exit" -eq 0 ]; then
    exit 1
  fi
  exit "$launch_exit"
fi

status_state="running"
write_status
log_message "search running; pid=${search_pid} runner_pid=${runner_pid}"

hard_deadline_epoch=$((deadline_epoch + shutdown_grace_seconds))
last_disk_check_epoch="0"
while /bin/kill -0 "$search_pid" 2>/dev/null; do
  now_epoch="$(/bin/date +%s)"

  if [ $((now_epoch - last_disk_check_epoch)) -ge "$disk_check_seconds" ]; then
    read_free_kib
    last_disk_check_epoch="$now_epoch"
    if [ "$free_kib" -lt "$minimum_free_kib" ]; then
      request_search_stop "free_disk_below_runtime_threshold"
    else
      write_status
    fi
  fi

  if [ "$now_epoch" -ge "$hard_deadline_epoch" ] && [ -z "$stop_reason" ]; then
    request_search_stop "duration_grace_exceeded"
  fi

  if [ -n "$stop_started_epoch" ]; then
    stop_elapsed=$((now_epoch - stop_started_epoch))
    if [ "$stop_elapsed" -ge 30 ] && [ "$stop_elapsed" -lt 45 ]; then
      /bin/kill -TERM "$search_pid" 2>/dev/null
    elif [ "$stop_elapsed" -ge 45 ]; then
      /bin/kill -KILL "$search_pid" 2>/dev/null
    fi
  fi

  /bin/sleep 1
done

wait "$runner_pid"
search_exit="$?"
read_free_kib

final_exit="$search_exit"
if [ -n "$signal_exit_code" ]; then
  final_exit="$signal_exit_code"
  status_state="interrupted"
  status_reason="$stop_reason"
elif [ "$stop_reason" = "free_disk_below_runtime_threshold" ]; then
  final_exit="75"
  status_state="stopped_low_disk"
  status_reason="$stop_reason"
elif [ "$stop_reason" = "duration_grace_exceeded" ]; then
  final_exit="124"
  status_state="timed_out"
  status_reason="$stop_reason"
elif [ "$search_exit" -eq 0 ]; then
  status_state="completed"
  status_reason="search_completed"
else
  status_state="failed"
  status_reason="search_process_failed"
fi
status_exit_code="$final_exit"
write_status
if [ -f "$catalog_builder" ]; then
  log_message "building final static catalog"
  if /usr/bin/env -i \
      HOME="${HOME:-/tmp}" \
      PATH="${PATH:-/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin}" \
      TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-C}" \
      /usr/bin/python3 "$catalog_builder" "$output_dir" --limit 324 \
      >> "$catalog_log" 2>&1; then
    log_message "final static catalog written to ${output_dir}/index.html"
  else
    log_message "final static catalog build failed; see ${catalog_log}"
  fi
fi
log_message "search finished; state=${status_state} exit=${final_exit}"
exit "$final_exit"
