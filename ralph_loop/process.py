"""Process execution and logging helpers."""
from __future__ import annotations

import datetime
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Sequence

from .errors import CommandError

MAX_CAPTURED_STREAM_BYTES = 1 << 20
_COMMAND_DEADLINE: Optional[float] = None
_DEFAULT_OUTPUT_LIMIT = object()
_JSON_LOG_PATH: Optional[str] = None


def _set_command_deadline(deadline: Optional[float]) -> Optional[float]:
    global _COMMAND_DEADLINE
    previous = _COMMAND_DEADLINE
    _COMMAND_DEADLINE = deadline
    return previous


def _configure_json_log(path: Optional[str]) -> Optional[str]:
    global _JSON_LOG_PATH
    previous = _JSON_LOG_PATH
    _JSON_LOG_PATH = path
    return previous


def _remaining_command_timeout(printable_cmd: str) -> Optional[float]:
    if _COMMAND_DEADLINE is None:
        return None
    remaining = _COMMAND_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise CommandError(
            "Wall-clock timeout exceeded before command: {}".format(printable_cmd)
        )
    return remaining


def _read_bounded_output(handle, limit: Optional[int] = MAX_CAPTURED_STREAM_BYTES) -> str:
    handle.flush()
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if limit is None:
        handle.seek(0)
        return handle.read().decode("utf-8", errors="replace")
    if size <= limit:
        handle.seek(0)
        return handle.read().decode("utf-8", errors="replace")
    head_size = max(0, limit // 2)
    tail_size = max(0, limit - head_size)
    handle.seek(0)
    head = handle.read(head_size)
    tail = b""
    if tail_size > 0:
        handle.seek(size - tail_size)
        tail = handle.read(tail_size)
    omitted = size - limit
    return "{}...<truncated {} bytes>...{}".format(
        head.decode("utf-8", errors="replace"),
        omitted,
        tail.decode("utf-8", errors="replace"),
    )


def _append_json_event(record: Dict[str, Any]) -> None:
    if not _JSON_LOG_PATH:
        return
    directory = os.path.dirname(os.path.abspath(_JSON_LOG_PATH))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(_JSON_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _print_step(message: str, event: str = "step", **fields: Any) -> None:
    now = datetime.datetime.now()
    timestamp = now.strftime("%H:%M:%S")
    sys.stderr.write("\n[{}] ==> {}\n".format(timestamp, message))
    sys.stderr.flush()
    if not _JSON_LOG_PATH:
        return
    record = {
        "event": event,
        "message": message,
        "timestamp": now.isoformat(timespec="seconds"),
    }
    record.update(fields)
    _append_json_event(record)


def _printable_cmd(cmd: Sequence[str], *, max_arg_len: int = 4000) -> str:
    parts = []
    for arg in cmd:
        if len(arg) > max_arg_len:
            parts.append(
                "{}...<+{} chars>".format(arg[:max_arg_len], len(arg) - max_arg_len)
            )
        else:
            parts.append(arg)
    return shlex.join(parts)


def _run_command(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    cwd: Optional[str] = None,
    input_text: Optional[str] = None,
    replay_output: bool = True,
    log_cmd: Optional[Sequence[str]] = None,
    max_output_bytes=_DEFAULT_OUTPUT_LIMIT,
) -> subprocess.CompletedProcess:
    printable = _printable_cmd(cmd)
    if log_cmd is None:
        logged = printable
    elif len(log_cmd) == 0:
        logged = ""
        printable = "<command redacted>"
    else:
        logged = _printable_cmd(log_cmd)
        printable = logged
    if logged:
        sys.stderr.write("$ {}\n".format(logged))
        sys.stderr.flush()
    timeout = _remaining_command_timeout(printable)
    output_limit = (
        MAX_CAPTURED_STREAM_BYTES
        if max_output_bytes is _DEFAULT_OUTPUT_LIMIT
        else max_output_bytes
    )
    if capture_output:
        stdout_spool = tempfile.TemporaryFile(mode="w+b")
        stderr_spool = tempfile.TemporaryFile(mode="w+b")
        try:
            try:
                completed = subprocess.run(  # nosec B603
                    list(cmd),
                    cwd=cwd,
                    stdout=stdout_spool,
                    stderr=stderr_spool,
                    input=input_text.encode("utf-8") if input_text is not None else None,
                    stdin=subprocess.DEVNULL if input_text is None else None,
                    check=False,
                    timeout=timeout,
                )
            except OSError as exc:
                raise CommandError(
                    "Unable to run command: {}: {}".format(printable, exc)
                ) from exc
            except subprocess.TimeoutExpired as exc:
                stdout_data = _read_bounded_output(stdout_spool, output_limit)
                stderr_data = _read_bounded_output(stderr_spool, output_limit)
                if replay_output and stdout_data:
                    sys.stdout.write(stdout_data)
                if replay_output and stderr_data:
                    sys.stderr.write(stderr_data)
                raise CommandError(
                    "Command timed out after {:.2f}s: {}".format(
                        float(exc.timeout), printable
                    )
                ) from exc
            stdout_data = _read_bounded_output(stdout_spool, output_limit)
            stderr_data = _read_bounded_output(stderr_spool, output_limit)
        finally:
            stdout_spool.close()
            stderr_spool.close()
        completed = subprocess.CompletedProcess(
            args=completed.args,
            returncode=completed.returncode,
            stdout=stdout_data,
            stderr=stderr_data,
        )
    else:
        try:
            completed = subprocess.run(  # nosec B603
                list(cmd),
                cwd=cwd,
                input=input_text,
                stdin=subprocess.DEVNULL if input_text is None else None,
                text=True,
                check=False,
                timeout=timeout,
            )
        except OSError as exc:
            raise CommandError(
                "Unable to run command: {}: {}".format(printable, exc)
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(
                "Command timed out after {:.2f}s: {}".format(
                    float(exc.timeout), printable
                )
            ) from exc
    if capture_output and replay_output:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
    if check and completed.returncode != 0:
        raise CommandError(
            "Command failed (exit={}): {}".format(completed.returncode, printable)
        )
    return completed


def _truncate_for_log(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return "{}...<truncated {} chars>...{}".format(
        text[:head], omitted, text[-tail:]
    )


def _completed_process_output(completed: subprocess.CompletedProcess) -> str:
    parts = []
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stdout and stderr and stdout == stderr:
        return "stdout+stderr:\n{}".format(stdout)
    if stdout:
        parts.append("stdout:\n{}".format(stdout))
    if stderr:
        parts.append("stderr:\n{}".format(stderr))
    return "\n\n".join(parts).strip()
