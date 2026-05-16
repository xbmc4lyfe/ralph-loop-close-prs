#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_SCRIPT="${RALPH_RANDOM_WORKER_SCRIPT:-$SCRIPT_DIR/run-ralph-random-repos-forever.sh}"
MULTI_LOG_DIR="${RALPH_RANDOM_MULTI_LOG_DIR:-$SCRIPT_DIR/.ralph-logs/random-repos-many}"
BASE_AGENT_ID="${RALPH_RANDOM_AGENT_ID:-agent}"
MAX_WORKERS="${RALPH_RANDOM_MAX_WORKERS:-50}"

usage() {
    echo "usage: $0 <worker-count> [start|status|stop]" >&2
}

is_non_negative_integer() {
    [[ "${1:-}" =~ ^[0-9]+$ ]]
}

count="${1:-}"
action="${2:-start}"

if ! is_non_negative_integer "$count" || [[ "$count" -le 0 ]]; then
    usage
    exit 2
fi

if [[ "$count" -gt "$MAX_WORKERS" ]]; then
    echo "refusing to launch ${count} workers; maximum is ${MAX_WORKERS}" >&2
    exit 2
fi

case "$action" in
    start|status|stop)
        ;;
    *)
        usage
        exit 2
        ;;
esac

if [[ ! -x "$WORKER_SCRIPT" ]]; then
    echo "worker script is not executable: $WORKER_SCRIPT" >&2
    exit 2
fi

mkdir -p "$MULTI_LOG_DIR"

for worker_number in $(seq 1 "$count"); do
    worker_id="$(printf "%04d" "$worker_number")"
    worker_log_dir="$MULTI_LOG_DIR/worker-$worker_id"
    mkdir -p "$worker_log_dir"
    output="$(
        RALPH_RANDOM_AGENT_ID="${BASE_AGENT_ID}-${worker_id}" \
        RALPH_RANDOM_LOG_DIR="$worker_log_dir" \
        "$WORKER_SCRIPT" "$action"
    )"
    if [[ -n "$output" ]]; then
        while IFS= read -r line; do
            echo "worker ${worker_id}: ${line}"
        done <<< "$output"
    else
        echo "worker ${worker_id}: ${action} completed"
    fi
done
