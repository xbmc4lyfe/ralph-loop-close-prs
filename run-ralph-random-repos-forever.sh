#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARCH_ROOT="${RALPH_RANDOM_ROOT:-$HOME/Documents/git}"
PROMPT_FILE="${RALPH_RANDOM_PROMPT_FILE:-$SCRIPT_DIR/.ralph-loop/random-repo-bugfix-prompt.md}"
LOG_DIR="${RALPH_RANDOM_LOG_DIR:-$SCRIPT_DIR/.ralph-logs/random-repos-active}"
OUT_FILE="$LOG_DIR/random-repos-forever.out"
PID_FILE="$LOG_DIR/random-repos-forever.pid"
LOCK_DIR="$LOG_DIR/random-repos-forever.lock"
WORKTREE_ROOT="${RALPH_RANDOM_WORKTREE_ROOT:-$LOG_DIR/worktrees}"
ITERATION_SECONDS="${RALPH_RANDOM_SECONDS:-60}"
SLEEP_SECONDS="${RALPH_RANDOM_SLEEP_SECONDS:-5}"
ERROR_SLEEP_SECONDS="${RALPH_RANDOM_ERROR_SLEEP_SECONDS:-30}"
MAX_DEPTH="${RALPH_RANDOM_MAX_DEPTH:-4}"
MODEL="${RALPH_RANDOM_MODEL:-gpt-5.5}"
REASONING_EFFORT="${RALPH_RANDOM_REASONING_EFFORT:-medium}"
SERVICE_TIER="${RALPH_RANDOM_SERVICE_TIER:-}"
INCLUDE_SELF="${RALPH_RANDOM_INCLUDE_SELF:-0}"
AGENT_ID="${RALPH_RANDOM_AGENT_ID:-agent-$$}"

mkdir -p "$LOG_DIR"

usage() {
    echo "usage: $0 start|run|status|stop" >&2
}

pid_is_live() {
    local pid
    pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

current_runner_pid() {
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if pid_is_live "$pid"; then
        echo "$pid"
        return 0
    fi
    pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if pid_is_live "$pid"; then
        echo "$pid"
        return 0
    fi
    return 1
}

release_run_lock() {
    local pid
    pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ "$pid" == "$$" ]]; then
        rm -rf "$LOCK_DIR"
    fi
}

cleanup_run() {
    local rc=$?
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" == "$$" && "$rc" -ne 0 && "$rc" -ne 130 && "$rc" -ne 143 ]]; then
        echo "[$(date)] runner exiting unexpectedly rc=${rc}" >> "$OUT_FILE"
    fi
    if [[ "$pid" == "$$" ]]; then
        rm -f "$PID_FILE"
    fi
    release_run_lock
}

acquire_run_lock() {
    local pid
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
        if pid_is_live "$pid"; then
            echo "already running $pid" >&2
            exit 0
        fi
        rm -rf "$LOCK_DIR"
    done
    echo "$$" > "$LOCK_DIR/pid"
}

is_running() {
    local pid
    pid="$(current_runner_pid || true)"
    [[ -n "$pid" ]] || return 1
    pid_is_live "$pid"
}

kill_descendants() {
    local parent="$1"
    local child
    for child in $(pgrep -P "$parent" 2>/dev/null || true); do
        kill_descendants "$child"
    done
    kill "$parent" 2>/dev/null || true
}

safe_name() {
    python3 - "$1" <<'PY'
import hashlib
import os
import re
import sys

path = sys.argv[1]
base = os.path.basename(path.rstrip(os.sep)) or "repo"
base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base)[:60]
digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
print(f"{base}-{digest}")
PY
}

safe_token() {
    python3 - "$1" <<'PY'
import re
import sys

token = sys.argv[1]
token = re.sub(r"[^A-Za-z0-9_.-]+", "_", token).strip("._-")
print((token or "agent")[:48])
PY
}

repo_is_clean() {
    local repo="$1"
    [[ -z "$(git -C "$repo" status --porcelain --untracked-files=all 2>/dev/null)" ]]
}

remove_worktree() {
    local source_repo="$1"
    local worktree="$2"
    local log_file="$3"
    local root
    local rc=0
    root="${WORKTREE_ROOT%/}"

    if [[ -z "$root" || -z "$worktree" || "$worktree" != "$root"/* ]]; then
        echo "Refusing to remove unexpected worktree path: $worktree" >> "$log_file"
        return 1
    fi

    git -C "$source_repo" worktree remove --force "$worktree" >> "$log_file" 2>&1 || rc=$?
    if [[ -d "$worktree" ]]; then
        echo "Removing leftover worktree directory after git worktree remove: $worktree" >> "$log_file"
        rm -rf "$worktree"
    fi
    if [[ "$rc" -ne 0 ]]; then
        git -C "$source_repo" worktree prune >> "$log_file" 2>&1 || true
    fi
}

kill_worktree_processes() {
    local worktree="$1"
    local log_file="$2"
    python3 - "$worktree" "$log_file" <<'PY'
import os
import signal
import subprocess
import sys
import time

worktree = os.path.abspath(os.path.expanduser(sys.argv[1]))
log_file = sys.argv[2]
needles = {worktree, os.path.realpath(worktree)}
own_pids = {os.getpid(), os.getppid()}


def matching_pids():
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = parts[1]
        if pid in own_pids:
            continue
        if any(needle and needle in command for needle in needles):
            pids.append(pid)
    return pids


def kill_matches(sig):
    pids = matching_pids()
    if not pids:
        return False
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(
            "Killing worktree-scoped processes with {}: {}\n".format(
                signal.Signals(sig).name,
                " ".join(str(pid) for pid in pids),
            )
        )
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (LookupError, PermissionError):
            pass
    return True


if kill_matches(signal.SIGTERM):
    time.sleep(0.5)
    kill_matches(signal.SIGKILL)
PY
}

pick_repo() {
    python3 - "$SEARCH_ROOT" "$MAX_DEPTH" "$SCRIPT_DIR" "$WORKTREE_ROOT" "$INCLUDE_SELF" <<'PY'
import os
import random
import subprocess
import sys

root = os.path.abspath(os.path.expanduser(sys.argv[1]))
max_depth = int(sys.argv[2])
script_dir = os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[3])))
worktree_root = os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[4])))
include_self = sys.argv[5] == "1"
repos = []
skip_roots = [worktree_root]
if not include_self:
    skip_roots.append(script_dir)

for current, dirs, files in os.walk(root):
    real_current = os.path.realpath(current)
    if any(real_current == skip or real_current.startswith(skip + os.sep) for skip in skip_roots):
        dirs[:] = []
        continue
    rel = os.path.relpath(current, root)
    depth = 0 if rel == "." else rel.count(os.sep) + 1
    has_git = ".git" in dirs or ".git" in files
    dirs[:] = [
        d for d in dirs
        if d not in {
            ".cache",
            ".codex",
            ".git",
            ".ralph-logs",
            ".venv",
            ".worktrees",
            "__pycache__",
            "node_modules",
            "target",
        }
    ]
    if depth > max_depth:
        dirs[:] = []
        continue
    if has_git:
        check = subprocess.run(
            ["git", "-C", current, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check.returncode == 0:
            repos.append(current)
        dirs[:] = []

if not repos:
    sys.exit(1)
print(random.choice(repos))
PY
}

run_codex_with_timeout() {
    local repo="$1"
    local context_file="$2"
    local last_message="$3"
    python3 - "$ITERATION_SECONDS" "$repo" "$context_file" "$last_message" "$MODEL" "$REASONING_EFFORT" "$SERVICE_TIER" <<'PY'
import os
import signal
import subprocess
import sys

seconds = int(sys.argv[1])
repo = sys.argv[2]
context_file = sys.argv[3]
last_message = sys.argv[4]
model = sys.argv[5]
reasoning_effort = sys.argv[6]
service_tier = sys.argv[7]
stdout = ""

cmd = [
    "codex",
    "exec",
    "--model",
    model,
    "-c",
    f"model_reasoning_effort=\"{reasoning_effort}\"",
    "--dangerously-bypass-approvals-and-sandbox",
    "--sandbox",
    "danger-full-access",
    "--cd",
    repo,
    "--output-last-message",
    last_message,
    "-",
]
if service_tier:
    cmd[cmd.index("--dangerously-bypass-approvals-and-sandbox"):cmd.index("--dangerously-bypass-approvals-and-sandbox")] = [
        "-c",
        f"service_tier=\"{service_tier}\"",
    ]

with open(context_file, "r", encoding="utf-8") as prompt:
    proc = subprocess.Popen(
        cmd,
        stdin=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

def matching_repo_pids():
    needles = {
        os.path.abspath(os.path.expanduser(repo)),
        os.path.realpath(os.path.abspath(os.path.expanduser(repo))),
    }
    own_pids = {os.getpid(), os.getppid()}
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = parts[1]
        if pid in own_pids:
            continue
        if any(needle and needle in command for needle in needles):
            pids.append(pid)
    return pids

def kill_repo_processes(sig):
    for pid in matching_repo_pids():
        try:
            os.kill(pid, sig)
        except (LookupError, PermissionError):
            pass

def kill_codex_group(sig):
    try:
        os.killpg(proc.pid, sig)
    except (LookupError, PermissionError):
        pass

try:
    stdout, _ = proc.communicate(timeout=seconds)
except subprocess.TimeoutExpired:
    kill_codex_group(signal.SIGTERM)
    kill_repo_processes(signal.SIGTERM)
    try:
        stdout, _ = proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        kill_repo_processes(signal.SIGKILL)
        kill_codex_group(signal.SIGKILL)
        try:
            stdout, _ = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout = ""
    sys.stdout.write(stdout or "")
    sys.stdout.write(f"\nRALPH_RANDOM_TIMEOUT after {seconds}s\n")
    sys.exit(124)

sys.stdout.write(stdout or "")
sys.exit(proc.returncode)
PY
}

write_context() {
    local repo="$1"
    local source_repo="$2"
    local branch_name="$3"
    local iteration="$4"
    local stamp="$5"
    local context_file="$6"
    {
        cat "$PROMPT_FILE"
        printf '\n\n<ralph_random_iteration_context>\n'
        printf 'iteration=%s\n' "$iteration"
        printf 'utc_started=%s\n' "$stamp"
        printf 'repo=%s\n' "$repo"
        printf 'source_repo=%s\n' "$source_repo"
        printf 'agent_id=%s\n' "$AGENT_ID"
        printf 'result_branch=%s\n' "$branch_name"
        printf 'search_root=%s\n' "$SEARCH_ROOT"
        printf 'time_budget_seconds=%s\n' "$ITERATION_SECONDS"
        printf '</ralph_random_iteration_context>\n'
    } > "$context_file"
}

run_loop() {
    if [[ ! -f "$PROMPT_FILE" ]]; then
        echo "Missing prompt file: $PROMPT_FILE" >&2
        exit 2
    fi

    acquire_run_lock
    echo "$$" > "$PID_FILE"
    trap cleanup_run EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    mkdir -p "$WORKTREE_ROOT"
    local iteration=0
    local agent_token
    agent_token="$(safe_token "$AGENT_ID")"
    while true; do
        iteration=$((iteration + 1))
        local stamp source_repo safe log_file last_message context_file worktree branch_name pre_head post_head status_after rc
        stamp="$(date -u '+%Y%m%dT%H%M%SZ')"

        if ! source_repo="$(pick_repo)"; then
            echo "[$(date)] no git repos found under $SEARCH_ROOT; sleeping ${ERROR_SLEEP_SECONDS}s" | tee -a "$OUT_FILE"
            sleep "$ERROR_SLEEP_SECONDS"
            continue
        fi

        safe="$(safe_name "$source_repo")"
        worktree="$WORKTREE_ROOT/iter-${stamp}-${iteration}-${agent_token}-${safe}"
        branch_name="ralph/random-${stamp}-${agent_token}-${safe}"
        log_file="$LOG_DIR/random-${stamp}-iter-${iteration}-${safe}.log"
        last_message="$LOG_DIR/random-${stamp}-iter-${iteration}-${safe}.last.md"
        context_file="$LOG_DIR/random-${stamp}-iter-${iteration}-${safe}.prompt.md"

        {
            echo "[$(date)] iteration=${iteration} source_repo=${source_repo}"
            echo "agent_id=${AGENT_ID}"
            echo "worktree=${worktree}"
            echo "result_branch=${branch_name}"
            echo "log=${log_file}"
        } | tee -a "$OUT_FILE"

        if ! git -C "$source_repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            echo "Skipping non-worktree repo path: $source_repo" | tee "$log_file"
            sleep "$SLEEP_SECONDS"
            continue
        fi

        if ! pre_head="$(git -C "$source_repo" rev-parse HEAD 2>/dev/null)"; then
            echo "Skipping repo without a valid HEAD: $source_repo" | tee "$log_file"
            sleep "$SLEEP_SECONDS"
            continue
        fi

        if ! repo_is_clean "$source_repo"; then
            {
                echo "Skipping repo that became dirty after selection: $source_repo"
                git -C "$source_repo" status --short --untracked-files=all | sed -n '1,80p'
            } | tee "$log_file"
            sleep "$SLEEP_SECONDS"
            continue
        fi

        if ! git -C "$source_repo" worktree add --detach "$worktree" "$pre_head" >> "$log_file" 2>&1; then
            {
                echo "Could not create isolated worktree for $source_repo"
                tail -80 "$log_file"
            } | tee -a "$log_file"
            sleep "$SLEEP_SECONDS"
            continue
        fi

        write_context "$worktree" "$source_repo" "$branch_name" "$iteration" "$stamp" "$context_file"

        set +e
        run_codex_with_timeout "$worktree" "$context_file" "$last_message" 2>&1 | tee -a "$log_file"
        rc=${PIPESTATUS[0]}
        set -e
        kill_worktree_processes "$worktree" "$log_file"

        if ! post_head="$(git -C "$worktree" rev-parse HEAD 2>>"$log_file")"; then
            {
                echo
                echo "Isolated worktree was unavailable after Codex; moving to next repo."
                echo "source_repo=${source_repo}"
                echo "worktree=${worktree}"
            } | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
            sleep "$SLEEP_SECONDS"
            continue
        fi
        status_after="$(git -C "$worktree" status --porcelain --untracked-files=all || true)"
        if [[ -n "$status_after" ]]; then
            {
                echo
                echo "Isolated worktree left dirty after iteration; cleaning Ralph's incomplete edits."
                git -C "$worktree" status --short --untracked-files=all
                git -C "$worktree" reset --hard "$post_head"
                git -C "$worktree" clean -fd
            } | tee -a "$log_file"
        fi

        if [[ "$post_head" != "$pre_head" ]]; then
            if ! git -C "$worktree" branch -f "$branch_name" "$post_head" >> "$log_file" 2>&1; then
                {
                    echo
                    echo "Could not save committed change for $source_repo on branch $branch_name."
                } | tee -a "$log_file"
                remove_worktree "$source_repo" "$worktree" "$log_file"
                sleep "$SLEEP_SECONDS"
                continue
            fi
            {
                echo
                echo "Committed change for $source_repo on branch $branch_name:"
                git -C "$worktree" log -1 --oneline
            } | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
        elif [[ "$rc" -eq 124 ]]; then
            echo "Iteration timed out with no committed change: $source_repo" | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
        elif [[ "$rc" -ne 0 ]]; then
            echo "Iteration exited rc=${rc} with no committed change: $source_repo" | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
        else
            echo "Iteration finished with no committed change: $source_repo" | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
        fi

        sleep "$SLEEP_SECONDS"
    done
}

case "${1:-start}" in
    start)
        if is_running; then
            echo "already running $(current_runner_pid)"
            exit 0
        fi
        rm -f "$PID_FILE"
        launcher="$SCRIPT_DIR/$(basename "$0")"
        if command -v setsid >/dev/null 2>&1; then
            setsid -f "$launcher" run < /dev/null > /dev/null 2>> "$OUT_FILE"
        else
            nohup "$launcher" run < /dev/null > /dev/null 2>> "$OUT_FILE" &
        fi
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if is_running; then
                echo "started $(current_runner_pid)"
                exit 0
            fi
            sleep 0.2
        done
        echo "start requested, but no live PID was confirmed; check $OUT_FILE" >&2
        exit 1
        ;;
    run)
        run_loop
        ;;
    status)
        if is_running; then
            echo "running $(current_runner_pid)"
        else
            echo "not running"
        fi
        echo "root: $SEARCH_ROOT"
        echo "logs: $LOG_DIR"
        tail -30 "$OUT_FILE" 2>/dev/null || true
        latest="$(find "$LOG_DIR" -maxdepth 1 -name 'random-*.log' -type f 2>/dev/null | sort | tail -1)"
        if [[ -n "${latest:-}" ]]; then
            echo "latest log: $latest"
            tail -40 "$latest" 2>/dev/null || true
        fi
        ;;
    stop)
        if is_running; then
            pid="$(current_runner_pid)"
            kill_descendants "$pid"
            rm -f "$PID_FILE"
            rm -rf "$LOCK_DIR"
            echo "stopped $pid"
        else
            echo "not running"
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac
