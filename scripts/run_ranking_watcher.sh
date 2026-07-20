#!/usr/bin/env bash

# Periodically rebuild an offline ranking while a search PID is alive.

set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run_dir="${1:-}"
watch_pid="${2:-}"
interval="${RANKING_INTERVAL_SECONDS:-300}"
pipeline="${SEARCH_RANKING_PIPELINE:-${script_dir}/build_ranked_catalog.sh}"
final_pass="${RANKING_FINAL_PASS:-1}"
stop_requested="0"
pipeline_pid=""
sleep_pid=""

handle_stop() {
  stop_requested="1"
  if [ -n "$pipeline_pid" ] && /bin/kill -0 "$pipeline_pid" 2>/dev/null; then
    /bin/kill -TERM "$pipeline_pid" 2>/dev/null
  fi
  if [ -n "$sleep_pid" ] && /bin/kill -0 "$sleep_pid" 2>/dev/null; then
    /bin/kill -TERM "$sleep_pid" 2>/dev/null
  fi
}
trap handle_stop INT TERM

case "$interval" in
  ''|*[!0-9]*) /bin/echo "RANKING_INTERVAL_SECONDS must be an integer" >&2; exit 2 ;;
esac
if [ "$interval" -lt 10 ]; then
  /bin/echo "RANKING_INTERVAL_SECONDS must be at least 10" >&2
  exit 2
fi
if [ -z "$run_dir" ] || [ ! -d "$run_dir" ]; then
  /bin/echo "Usage: $0 SEARCH_RUN_DIR [SEARCH_PID]" >&2
  exit 2
fi
if [ ! -x "$pipeline" ]; then
  /bin/echo "Ranking pipeline is not executable: ${pipeline}" >&2
  exit 2
fi
if [ -n "$watch_pid" ]; then
  case "$watch_pid" in
    *[!0-9]*) /bin/echo "SEARCH_PID must be an integer" >&2; exit 2 ;;
  esac
fi

run_once() {
  /usr/bin/printf '%s ranking snapshot started\n' "$(/bin/date -u +'%Y-%m-%dT%H:%M:%SZ')"
  "$pipeline" "$run_dir" &
  pipeline_pid="$!"
  wait "$pipeline_pid"
  status="$?"
  pipeline_pid=""
  if [ "$status" -eq 0 ]; then
    /usr/bin/printf '%s ranking snapshot completed\n' "$(/bin/date -u +'%Y-%m-%dT%H:%M:%SZ')"
    return 0
  else
    /usr/bin/printf '%s ranking snapshot failed exit=%s; previous atomic reports preserved\n' \
      "$(/bin/date -u +'%Y-%m-%dT%H:%M:%SZ')" "$status" >&2
    return "$status"
  fi
}

run_once
last_status="$?"
if [ -z "$watch_pid" ] || [ "$stop_requested" = "1" ]; then
  exit "$last_status"
fi

while /bin/kill -0 "$watch_pid" 2>/dev/null; do
  /bin/sleep "$interval" &
  sleep_pid="$!"
  wait "$sleep_pid" 2>/dev/null
  sleep_pid=""
  if [ "$stop_requested" = "1" ]; then
    break
  fi
  if ! /bin/kill -0 "$watch_pid" 2>/dev/null; then
    break
  fi
  run_once
  last_status="$?"
done

# The search archive is now stable, so publish one final complete snapshot.
if [ "$stop_requested" = "1" ]; then
  exit "$last_status"
fi
case "$final_pass" in
  1|true|TRUE|yes|YES|on|ON)
    run_once
    exit "$?"
    ;;
  *)
    exit "$last_status"
    ;;
esac
