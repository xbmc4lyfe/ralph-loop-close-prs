import os
import signal
import stat
import subprocess
import time
from pathlib import Path


def _run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def _write_executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _pid_is_live(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _logs_contain(log_dir, text):
    return any(text in path.read_text() for path in log_dir.glob("random-*.log"))


def test_start_writes_iteration_header_once(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "run-ralph-random-repos-forever.sh"
    search_root = tmp_path / "repos"
    source_repo = search_root / "sample"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    out_file = log_dir / "random-repos-forever.out"

    source_repo.mkdir(parents=True)
    _run(["git", "init"], cwd=str(source_repo), stdout=subprocess.DEVNULL)
    (source_repo / "README.md").write_text("sample\n")
    _run(["git", "add", "README.md"], cwd=str(source_repo))
    _run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=str(source_repo),
        stdout=subprocess.DEVNULL,
    )

    prompt.write_text("fake prompt\n")
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "codex",
        """#!/usr/bin/env bash
set -euo pipefail
last_message=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-last-message)
      shift
      last_message="$1"
      ;;
  esac
  shift || true
done
printf 'fake codex iteration\\n'
if [[ -n "$last_message" ]]; then
  printf 'RALPH_RANDOM_RESULT=no_bug_found\\n' > "$last_message"
fi
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(search_root),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "5",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if out_file.exists() and "source_repo=" in out_file.read_text():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not write an iteration header")
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)

    out = out_file.read_text()
    source_lines = [line for line in out.splitlines() if "source_repo=" in line]
    assert source_lines == [
        "[{}] iteration=1 source_repo={}".format(
            source_lines[0].split("] ", 1)[0].strip("["), source_repo
        )
    ]


def test_picker_does_not_run_status_before_iteration_header(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "run-ralph-random-repos-forever.sh"
    search_root = tmp_path / "repos"
    source_repo = search_root / "sample"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    call_log = tmp_path / "git-calls.log"
    out_file = log_dir / "random-repos-forever.out"

    (source_repo / ".git").mkdir(parents=True)
    prompt.write_text("fake prompt\n")
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
set -euo pipefail
repo=""
if [[ "${1:-}" == "-C" ]]; then
  repo="$2"
  shift 2
fi
cmd="${1:-}"
case "$cmd" in
  rev-parse)
    if [[ "${2:-}" == "--is-inside-work-tree" ]]; then
      exit 0
    fi
    if [[ "${2:-}" == "HEAD" ]]; then
      printf '1111111111111111111111111111111111111111\\n'
      exit 0
    fi
    ;;
  status)
    if [[ -s "${RALPH_TEST_OUT_FILE}" ]]; then
      printf 'status_after_header %s\\n' "$repo" >> "${RALPH_TEST_CALL_LOG}"
    else
      printf 'status_before_header %s\\n' "$repo" >> "${RALPH_TEST_CALL_LOG}"
    fi
    exit 0
    ;;
  worktree)
    if [[ "${2:-}" == "add" ]]; then
      mkdir -p "$5"
      exit 0
    fi
    if [[ "${2:-}" == "remove" ]]; then
      rm -rf "$4"
      exit 0
    fi
    ;;
  reset|clean)
    exit 0
    ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "codex",
        """#!/usr/bin/env bash
set -euo pipefail
last_message=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-last-message)
      shift
      last_message="$1"
      ;;
  esac
  shift || true
done
printf 'fake codex iteration\\n'
if [[ -n "$last_message" ]]; then
  printf 'RALPH_RANDOM_RESULT=no_bug_found\\n' > "$last_message"
fi
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(search_root),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "5",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
            "RALPH_TEST_CALL_LOG": str(call_log),
            "RALPH_TEST_OUT_FILE": str(out_file),
        }
    )

    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if call_log.exists() and "status_after_header" in call_log.read_text():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not reach post-selection status check")
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)

    calls = call_log.read_text().splitlines()
    assert "status_before_header {}".format(source_repo) not in calls
    assert "status_after_header {}".format(source_repo) in calls


def test_timeout_kills_processes_scoped_to_iteration_worktree(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "run-ralph-random-repos-forever.sh"
    search_root = tmp_path / "repos"
    source_repo = search_root / "sample"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    rogue_pid_file = log_dir / "rogue.pid"
    rogue_pid = None

    source_repo.mkdir(parents=True)
    _run(["git", "init"], cwd=str(source_repo), stdout=subprocess.DEVNULL)
    (source_repo / "README.md").write_text("sample\n")
    _run(["git", "add", "README.md"], cwd=str(source_repo))
    _run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=str(source_repo),
        stdout=subprocess.DEVNULL,
    )

    prompt.write_text("fake prompt\n")
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "codex",
        """#!/usr/bin/env bash
set -euo pipefail
repo=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cd)
      shift
      repo="$1"
      ;;
  esac
  shift || true
done
setsid python3 -c 'import sys,time; time.sleep(30)' "$repo" &
printf '%s' "$!" > "$RALPH_RANDOM_LOG_DIR/rogue.pid"
sleep 30
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(search_root),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "1",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if rogue_pid_file.exists():
                rogue_pid = int(rogue_pid_file.read_text())
                break
            time.sleep(0.1)
        else:
            raise AssertionError("fake codex did not create rogue child")

        deadline = time.time() + 10
        while time.time() < deadline:
            if _logs_contain(log_dir, "Iteration timed out"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not record the timeout")
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)

    assert rogue_pid is not None
    try:
        assert not _pid_is_live(rogue_pid)
    finally:
        if _pid_is_live(rogue_pid):
            os.kill(rogue_pid, signal.SIGKILL)
