"""Process execution and logging helpers."""
from __future__ import annotations

import datetime
import shlex
import subprocess
import sys
import tempfile
from typing import Optional, Sequence

from .errors import CommandError

def _print_step(message: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    sys.stderr.write("\n[{}] ==> {}\n".format(timestamp, message))
    sys.stderr.flush()


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
) -> subprocess.CompletedProcess:
    printable = _printable_cmd(cmd)
    sys.stderr.write("$ {}\n".format(printable))
    sys.stderr.flush()
    if capture_output:
        stdout_spool = tempfile.SpooledTemporaryFile(
            max_size=1 << 20, mode="w+", encoding="utf-8"
        )
        stderr_spool = tempfile.SpooledTemporaryFile(
            max_size=1 << 20, mode="w+", encoding="utf-8"
        )
        try:
            completed = subprocess.run(  # nosec B603
                list(cmd),
                cwd=cwd,
                text=True,
                stdout=stdout_spool,
                stderr=stderr_spool,
                check=False,
            )
            stdout_spool.seek(0)
            stderr_spool.seek(0)
            stdout_data = stdout_spool.read()
            stderr_data = stderr_spool.read()
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
        completed = subprocess.run(  # nosec B603
            list(cmd),
            cwd=cwd,
            text=True,
            check=False,
        )
    if capture_output:
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
