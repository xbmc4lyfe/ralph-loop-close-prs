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


def test_start_detaches_and_records_runner_pid(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "run-ralph-random-repos-forever.sh"
    search_root = tmp_path / "repos"
    source_repo = search_root / "sample"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    pid_file = log_dir / "random-repos-forever.pid"

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
            "RALPH_RANDOM_SECONDS": "30",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    try:
        result = _run(
            [str(script), "start"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.stdout.startswith("started ")

        deadline = time.time() + 10
        while time.time() < deadline:
            if pid_file.exists():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not write its pid file")

        runner_pid = int(pid_file.read_text())
        assert result.stdout.strip() == "started {}".format(runner_pid)
        assert _pid_is_live(runner_pid)
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)


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
    assert "selecting_repo iteration=1 root={}".format(search_root) in out
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
    other_repo = search_root / "other"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    call_log = tmp_path / "git-calls.log"
    out_file = log_dir / "random-repos-forever.out"

    (source_repo / ".git").mkdir(parents=True)
    (other_repo / ".git").mkdir(parents=True)
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
      if grep -q 'source_repo=' "${RALPH_TEST_OUT_FILE}" 2>/dev/null; then
        printf 'probe_after_source_header %s\\n' "$repo" >> "${RALPH_TEST_CALL_LOG}"
      else
        printf 'probe_before_source_header %s\\n' "$repo" >> "${RALPH_TEST_CALL_LOG}"
      fi
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
            "RALPH_RANDOM_ROOT": str(source_repo),
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
    source_line = next(
        line for line in out_file.read_text().splitlines() if "source_repo=" in line
    )
    selected_repo = source_line.split("source_repo=", 1)[1]
    assert "status_before_header {}".format(selected_repo) not in calls
    assert "status_after_header {}".format(selected_repo) in calls
    before_source_probes = [
        line for line in calls if line.startswith("probe_before_source_header ")
    ]
    assert len(before_source_probes) == 1


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
        deadline = time.time() + 30
        while time.time() < deadline:
            if rogue_pid_file.exists():
                rogue_pid = int(rogue_pid_file.read_text())
                break
            time.sleep(0.1)
        else:
            raise AssertionError("fake codex did not create rogue child")

        deadline = time.time() + 30
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


def test_timeout_advances_to_next_iteration(tmp_path):
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
sleep 30
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(source_repo),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "1",
            "RALPH_RANDOM_SLEEP_SECONDS": "1",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    proc = subprocess.Popen(
        [str(script), "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 12
        while time.time() < deadline:
            if out_file.exists() and "selecting_repo iteration=2" in out_file.read_text():
                break
            if proc.poll() is not None:
                raise AssertionError(
                    "runner exited before continuing after a timed-out iteration"
                )
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not continue after a timed-out iteration")
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)

    out = out_file.read_text()
    assert "Iteration timed out with no committed change" in out
    assert "selecting_repo iteration=2" in out


def test_usage_limit_response_backs_off_instead_of_advancing(tmp_path):
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
printf 'ERROR: You have hit your usage limit. Visit settings to purchase more credits or try again later.\\n'
exit 1
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(source_repo),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "5",
            "RALPH_RANDOM_SLEEP_SECONDS": "1",
            "RALPH_RANDOM_USAGE_LIMIT_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 15
        while time.time() < deadline:
            if out_file.exists() and "codex_usage_limited iteration=1; sleeping 60s" in out_file.read_text():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not back off after Codex usage limit")

        time.sleep(1.5)
        out = out_file.read_text()
        assert "selecting_repo iteration=2" not in out
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)


def test_setup_timeout_prevents_codex_from_starting(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "run-ralph-random-repos-forever.sh"
    search_root = tmp_path / "repos"
    source_repo = search_root / "sample"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    call_log = tmp_path / "calls.log"

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
    exit 0
    ;;
  worktree)
    if [[ "${2:-}" == "add" ]]; then
      sleep 2
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
printf 'codex_started\\n' >> "${RALPH_TEST_CALL_LOG}"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(source_repo),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "1",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
            "RALPH_TEST_CALL_LOG": str(call_log),
        }
    )

    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 8
        while time.time() < deadline:
            if _logs_contain(log_dir, "Iteration setup timed out"):
                break
            if call_log.exists() and "codex_started" in call_log.read_text():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not finish setup-timeout scenario")
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)

    assert _logs_contain(log_dir, "Iteration setup timed out")
    assert not call_log.exists()


def test_sigterm_cleans_active_iteration_worktree(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "run-ralph-random-repos-forever.sh"
    search_root = tmp_path / "repos"
    source_repo = search_root / "sample"
    log_dir = tmp_path / "logs"
    prompt = tmp_path / "prompt.md"
    fake_bin = tmp_path / "bin"
    out_file = log_dir / "random-repos-forever.out"
    pid_file = log_dir / "random-repos-forever.pid"

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
sleep 30
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "RALPH_RANDOM_LOG_DIR": str(log_dir),
            "RALPH_RANDOM_ROOT": str(source_repo),
            "RALPH_RANDOM_PROMPT_FILE": str(prompt),
            "RALPH_RANDOM_SECONDS": "30",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    worktree = None
    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if out_file.exists():
                for line in out_file.read_text().splitlines():
                    if line.startswith("worktree="):
                        worktree = Path(line.split("=", 1)[1])
                        break
            if worktree is not None and worktree.exists() and pid_file.exists():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not enter a Codex iteration")

        pid = int(pid_file.read_text())
        os.kill(pid, signal.SIGTERM)

        deadline = time.time() + 10
        while time.time() < deadline:
            if not _pid_is_live(pid):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not exit after SIGTERM")
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)

    assert worktree is not None
    assert not worktree.exists()


def test_stop_cleans_active_iteration_worktree(tmp_path):
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
            "RALPH_RANDOM_SECONDS": "30",
            "RALPH_RANDOM_SLEEP_SECONDS": "60",
            "RALPH_RANDOM_AGENT_ID": "test-agent",
        }
    )

    worktree = None
    try:
        _run([str(script), "start"], env=env, stdout=subprocess.PIPE)
        deadline = time.time() + 15
        while time.time() < deadline:
            if out_file.exists():
                for line in out_file.read_text().splitlines():
                    if line.startswith("worktree="):
                        worktree = Path(line.split("=", 1)[1])
                        break
            if worktree is not None and worktree.exists():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("runner did not create an active worktree")
    finally:
        subprocess.run([str(script), "stop"], env=env, stdout=subprocess.PIPE, text=True)

    assert worktree is not None
    deadline = time.time() + 10
    while time.time() < deadline:
        if not worktree.exists():
            break
        time.sleep(0.1)

    assert not worktree.exists()
