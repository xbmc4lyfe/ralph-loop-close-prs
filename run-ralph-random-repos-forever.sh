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
USAGE_LIMIT_SLEEP_SECONDS="${RALPH_RANDOM_USAGE_LIMIT_SLEEP_SECONDS:-1800}"
STOP_GRACE_SECONDS="${RALPH_RANDOM_STOP_GRACE_SECONDS:-15}"
MAX_DEPTH="${RALPH_RANDOM_MAX_DEPTH:-4}"
MODEL="${RALPH_RANDOM_MODEL:-gpt-5.5}"
REASONING_EFFORT="${RALPH_RANDOM_REASONING_EFFORT:-medium}"
SERVICE_TIER="${RALPH_RANDOM_SERVICE_TIER:-}"
INCLUDE_SELF="${RALPH_RANDOM_INCLUDE_SELF:-0}"
AGENT_ID="${RALPH_RANDOM_AGENT_ID:-agent-$$}"
ACTIVE_SOURCE_REPO=""
ACTIVE_WORKTREE=""
ACTIVE_LOG_FILE=""
ACTIVE_CHILD_PID=""

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
    cleanup_active_iteration
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" == "$$" && "$rc" -ne 0 && "$rc" -ne 130 && "$rc" -ne 143 ]]; then
        echo "[$(date)] runner exiting unexpectedly rc=${rc}" >> "$OUT_FILE"
    fi
    if [[ "$pid" == "$$" ]]; then
        rm -f "$PID_FILE"
    fi
    release_run_lock
}

cleanup_active_iteration() {
    cleanup_active_child
    if [[ -z "$ACTIVE_SOURCE_REPO" || -z "$ACTIVE_WORKTREE" || -z "$ACTIVE_LOG_FILE" ]]; then
        return 0
    fi
    if [[ ! -d "$ACTIVE_WORKTREE" ]]; then
        ACTIVE_SOURCE_REPO=""
        ACTIVE_WORKTREE=""
        ACTIVE_LOG_FILE=""
        return 0
    fi
    {
        echo
        echo "Cleaning active iteration worktree after runner shutdown."
        echo "source_repo=${ACTIVE_SOURCE_REPO}"
        echo "worktree=${ACTIVE_WORKTREE}"
    } >> "$ACTIVE_LOG_FILE"
    kill_worktree_processes "$ACTIVE_WORKTREE" "$ACTIVE_LOG_FILE" || true
    remove_worktree "$ACTIVE_SOURCE_REPO" "$ACTIVE_WORKTREE" "$ACTIVE_LOG_FILE" || true
    ACTIVE_SOURCE_REPO=""
    ACTIVE_WORKTREE=""
    ACTIVE_LOG_FILE=""
}

cleanup_active_child() {
    if [[ -z "$ACTIVE_CHILD_PID" ]]; then
        return 0
    fi
    kill "$ACTIVE_CHILD_PID" 2>/dev/null || true
    if ! wait_for_pid_exit "$ACTIVE_CHILD_PID" 2 2>/dev/null; then
        kill_descendants "$ACTIVE_CHILD_PID" || true
        kill -KILL "$ACTIVE_CHILD_PID" 2>/dev/null || true
        wait_for_pid_exit "$ACTIVE_CHILD_PID" 1 2>/dev/null || true
    fi
    kill_descendants "$ACTIVE_CHILD_PID" || true
    ACTIVE_CHILD_PID=""
}

handle_runner_signal() {
    local rc="$1"
    trap - INT TERM
    cleanup_active_iteration || true
    exit "$rc"
}

clear_active_iteration() {
    ACTIVE_CHILD_PID=""
    ACTIVE_SOURCE_REPO=""
    ACTIVE_WORKTREE=""
    ACTIVE_LOG_FILE=""
}

run_interruptible() {
    "$@" &
    ACTIVE_CHILD_PID="$!"
    wait "$ACTIVE_CHILD_PID"
    local rc=$?
    ACTIVE_CHILD_PID=""
    return "$rc"
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

wait_for_pid_exit() {
    local pid="$1"
    local seconds="$2"
    python3 - "$pid" "$seconds" <<'PY'
import os
import sys
import time

pid = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])

while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        sys.exit(0)
    except PermissionError:
        pass
    time.sleep(0.2)

try:
    os.kill(pid, 0)
except ProcessLookupError:
    sys.exit(0)
except PermissionError:
    pass
sys.exit(1)
PY
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

codex_usage_limited() {
    local log_file="$1"
    grep -Eiq 'usage limit|purchase more credits|try again at|exceeded retry limit|429 Too Many Requests|401 Unauthorized|invalid api key|Missing bearer or basic authentication|502 Bad Gateway' "$log_file" 2>/dev/null
}

monotonic_millis() {
    python3 - <<'PY'
import time

print(int(time.monotonic() * 1000))
PY
}

remaining_iteration_seconds() {
    local started_ms="$1"
    python3 - "$started_ms" "$ITERATION_SECONDS" <<'PY'
import sys
import time

started_ms = int(sys.argv[1])
budget_ms = int(float(sys.argv[2]) * 1000)
elapsed_ms = int(time.monotonic() * 1000) - started_ms
remaining_ms = max(0, budget_ms - elapsed_ms)
print("{:.3f}".format(remaining_ms / 1000.0))
PY
}

has_remaining_time() {
    python3 - "$1" <<'PY'
import sys

sys.exit(0 if float(sys.argv[1]) > 0 else 1)
PY
}

run_setup_with_timeout() {
    local seconds="$1"
    local log_file="$2"
    shift 2
    python3 - "$seconds" "$log_file" "$@" <<'PY'
import os
import signal
import subprocess
import sys

seconds = float(sys.argv[1])
log_file = sys.argv[2]
cmd = sys.argv[3:]

if seconds <= 0:
    sys.exit(124)

with open(log_file, "a", encoding="utf-8") as log:
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        sys.exit(proc.wait(timeout=seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        sys.exit(124)
PY
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
log_dir = os.path.dirname(os.path.abspath(log_file))
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


def logged_pids():
    pids = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return pids
    for name in names:
        if name == "random-repos-forever.pid":
            continue
        if not name.endswith(".pid"):
            continue
        path = os.path.join(log_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if not text.isdigit():
            continue
        pid = int(text)
        if pid in own_pids:
            continue
        pids.append(pid)
    return pids


def kill_matches(sig):
    pids = sorted(set(matching_pids() + logged_pids()))
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
        except (ProcessLookupError, PermissionError):
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
node_budget = int(os.environ.get("RALPH_RANDOM_PICKER_NODE_BUDGET", "500"))
candidate_limit = int(os.environ.get("RALPH_RANDOM_PICKER_CANDIDATE_LIMIT", "64"))
skip_roots = [worktree_root]
if not include_self:
    skip_roots.append(script_dir)
prune_names = {
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


def should_skip(path):
    real_path = os.path.realpath(path)
    return any(real_path == skip or real_path.startswith(skip + os.sep) for skip in skip_roots)


def depth_for(path):
    rel = os.path.relpath(path, root)
    return 0 if rel == "." else rel.count(os.sep) + 1


stack = [root]
visited = 0
candidates = []
while stack and visited < node_budget and len(candidates) < candidate_limit:
    index = random.randrange(len(stack))
    current = stack.pop(index)
    visited += 1
    if should_skip(current):
        continue
    depth = depth_for(current)
    if depth > max_depth:
        continue

    try:
        entries = os.listdir(current)
    except OSError:
        continue

    if ".git" in entries:
        candidates.append(current)
        continue

    if depth == max_depth:
        continue

    children = []
    for name in entries:
        if name in prune_names:
            continue
        path = os.path.join(current, name)
        if os.path.isdir(path):
            children.append(path)
    random.shuffle(children)
    stack.extend(children)

random.shuffle(candidates)
for current in candidates:
    check = subprocess.run(
        ["git", "-C", current, "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode == 0:
        print(current)
        sys.exit(0)

sys.exit(1)
PY
}

run_codex_with_timeout() {
    local seconds="$1"
    local repo="$2"
    local context_file="$3"
    local last_message="$4"
    local log_file="$5"
    python3 - "$seconds" "$repo" "$context_file" "$last_message" "$log_file" "$MODEL" "$REASONING_EFFORT" "$SERVICE_TIER" <<'PY'
import os
import signal
import subprocess
import sys
import threading

seconds = float(sys.argv[1])
repo = sys.argv[2]
context_file = sys.argv[3]
last_message = sys.argv[4]
log_file = sys.argv[5]
log_dir = os.path.dirname(os.path.abspath(log_file))
model = sys.argv[6]
reasoning_effort = sys.argv[7]
service_tier = sys.argv[8]

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

def copy_output():
    assert proc.stdout is not None
    with open(log_file, "a", encoding="utf-8") as log:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            sys.stdout.write(chunk)
            sys.stdout.flush()
            log.write(chunk)
            log.flush()

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

def logged_pids():
    pids = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return pids
    for name in names:
        if name == "random-repos-forever.pid":
            continue
        if not name.endswith(".pid"):
            continue
        path = os.path.join(log_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if not text.isdigit():
            continue
        pid = int(text)
        if pid in {os.getpid(), os.getppid(), proc.pid}:
            continue
        pids.append(pid)
    return pids

def kill_repo_processes(sig):
    for pid in sorted(set(matching_repo_pids() + logged_pids())):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

def kill_codex_group(sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass

def handle_shutdown(signum, _frame):
    kill_codex_group(signum)
    kill_repo_processes(signum)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        kill_repo_processes(signal.SIGKILL)
        kill_codex_group(signal.SIGKILL)
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

reader = threading.Thread(target=copy_output, daemon=True)
reader.start()

try:
    proc.wait(timeout=seconds)
except subprocess.TimeoutExpired:
    kill_codex_group(signal.SIGTERM)
    kill_repo_processes(signal.SIGTERM)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        kill_repo_processes(signal.SIGKILL)
        kill_codex_group(signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=1)
    message = f"\nRALPH_RANDOM_TIMEOUT after {seconds:.3f}s\n"
    sys.stdout.write(message)
    sys.stdout.flush()
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(message)
    sys.exit(124)

reader.join(timeout=1)
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
    trap 'handle_runner_signal 130' INT
    trap 'handle_runner_signal 143' TERM

    mkdir -p "$WORKTREE_ROOT"
    local iteration=0
    local agent_token
    agent_token="$(safe_token "$AGENT_ID")"
    while true; do
        iteration=$((iteration + 1))
        local stamp iteration_started source_repo safe log_file last_message context_file worktree branch_name pre_head post_head status_after rc setup_remaining codex_remaining
        stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
        iteration_started="$(monotonic_millis)"

        echo "[$(date)] selecting_repo iteration=${iteration} root=${SEARCH_ROOT}" | tee -a "$OUT_FILE"
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

        ACTIVE_SOURCE_REPO="$source_repo"
        ACTIVE_WORKTREE="$worktree"
        ACTIVE_LOG_FILE="$log_file"

        setup_remaining="$(remaining_iteration_seconds "$iteration_started")"
        set +e
        run_interruptible run_setup_with_timeout "$setup_remaining" "$log_file" git -C "$source_repo" worktree add --detach "$worktree" "$pre_head"
        rc=$?
        set -e
        if [[ "$rc" -ne 0 ]]; then
            if [[ "$rc" -eq 124 ]]; then
                echo "Iteration setup timed out after ${ITERATION_SECONDS}s: $source_repo" | tee -a "$log_file"
                remove_worktree "$source_repo" "$worktree" "$log_file"
                clear_active_iteration
                sleep "$SLEEP_SECONDS"
                continue
            fi
            {
                echo "Could not create isolated worktree for $source_repo"
                tail -80 "$log_file"
            } | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
            clear_active_iteration
            sleep "$SLEEP_SECONDS"
            continue
        fi

        write_context "$worktree" "$source_repo" "$branch_name" "$iteration" "$stamp" "$context_file"

        codex_remaining="$(remaining_iteration_seconds "$iteration_started")"
        if ! has_remaining_time "$codex_remaining"; then
            echo "Iteration setup timed out after ${ITERATION_SECONDS}s: $source_repo" | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
            clear_active_iteration
            sleep "$SLEEP_SECONDS"
            continue
        fi

        set +e
        run_interruptible run_codex_with_timeout "$codex_remaining" "$worktree" "$context_file" "$last_message" "$log_file"
        rc=$?
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
            clear_active_iteration
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
            clear_active_iteration
        elif [[ "$rc" -eq 124 ]]; then
            echo "Iteration timed out with no committed change: $source_repo" | tee -a "$OUT_FILE" "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
            clear_active_iteration
        elif [[ "$rc" -ne 0 ]]; then
            if codex_usage_limited "$log_file"; then
                {
                    echo "codex_usage_limited iteration=${iteration}; sleeping ${USAGE_LIMIT_SLEEP_SECONDS}s"
                    echo "source_repo=${source_repo}"
                } | tee -a "$OUT_FILE" "$log_file"
                remove_worktree "$source_repo" "$worktree" "$log_file"
                clear_active_iteration
                sleep "$USAGE_LIMIT_SLEEP_SECONDS"
                continue
            fi
            echo "Iteration exited rc=${rc} with no committed change: $source_repo" | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
            clear_active_iteration
        else
            echo "Iteration finished with no committed change: $source_repo" | tee -a "$log_file"
            remove_worktree "$source_repo" "$worktree" "$log_file"
            clear_active_iteration
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
            kill "$pid" 2>/dev/null || true
            if ! wait_for_pid_exit "$pid" "$STOP_GRACE_SECONDS"; then
                echo "runner did not exit within ${STOP_GRACE_SECONDS}s; killing descendants" >&2
            fi
            if pid_is_live "$pid"; then
                kill_descendants "$pid"
            fi
            if pid_is_live "$pid"; then
                kill -KILL "$pid" 2>/dev/null || true
            fi
            wait_for_pid_exit "$pid" 2>/dev/null || true
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
