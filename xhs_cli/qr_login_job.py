"""Detached QR login worker for non-streaming command runners."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .constants import QR_LOGIN_STATE_FILE
from .cookies import get_config_dir, get_cookie_path, set_cookie_path
from .qr_login import qrcode_login

_ACTIVE_STATUSES = {"starting", "waiting", "scanned", "confirmed"}
_TERMINAL_STATUSES = {"succeeded", "failed"}
_WORKER_EXPIRY_GRACE_S = 60


def get_qr_login_state_path() -> Path:
    """Return the detached QR login state file path."""
    return get_config_dir() / QR_LOGIN_STATE_FILE


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"status": "not_started"}
    return data if isinstance(data, dict) else {"status": "not_started"}


def get_qr_login_status(*, state_file: Path | None = None) -> dict[str, Any]:
    """Read and reconcile the latest detached QR login status."""
    path = state_file or get_qr_login_state_path()
    state = _read_state(path)
    if state.get("status") not in _ACTIVE_STATUSES:
        return state

    now = time.time()
    expires_at = float(state.get("expires_at", 0) or 0)
    startup_deadline = float(state.get("startup_deadline", 0) or 0)
    if expires_at and now >= expires_at:
        message = "QR login worker expired before login completed"
    elif state.get("status") == "starting" and not state.get("pid") and now < startup_deadline:
        return state
    elif _process_is_running(state.get("pid")):
        return state
    else:
        message = "QR login worker stopped before login completed"

    failed = {**state, "status": "failed", "updated_at": now, "message": message}
    current = _read_state(path)
    if current.get("job_id") == state.get("job_id") and current.get("status") in _ACTIVE_STATUSES:
        _write_state(path, failed)
    return failed


def _process_is_running(pid: object) -> bool:
    if not isinstance(pid, (int, str)):
        return False
    try:
        process_id = int(pid)
        if process_id <= 0:
            return False
        os.kill(process_id, 0)
        stat_path = Path(f"/proc/{process_id}/stat")
        if stat_path.exists() and stat_path.read_text(encoding="utf-8").split()[2] == "Z":
            return False
    except (OSError, TypeError, ValueError, IndexError):
        return False
    return True


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    """Stop and reap the worker spawned by this command."""
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def start_qr_login(
    *,
    state_file: Path | None = None,
    cookie_file: Path | None = None,
    timeout_s: int = 240,
    startup_timeout_s: float = 30,
) -> dict[str, Any]:
    """Start a detached QR worker and wait only until its QR URL is ready."""
    state_path = state_file or get_qr_login_state_path()
    cookie_path = cookie_file or get_cookie_path()
    current = get_qr_login_status(state_file=state_path)
    if current.get("status") in _ACTIVE_STATUSES:
        raise RuntimeError("A QR login is already in progress. Run `xhs login --qrcode-status`.")

    job_id = uuid.uuid4().hex
    started_at = time.time()
    _write_state(
        state_path,
        {
            "job_id": job_id,
            "status": "starting",
            "started_at": started_at,
            "startup_deadline": started_at + startup_timeout_s,
            "expires_at": started_at + startup_timeout_s + timeout_s + _WORKER_EXPIRY_GRACE_S,
        },
    )
    command = [
        sys.executable,
        "-m",
        "xhs_cli.qr_login_job",
        "--worker",
        "--state-file",
        str(state_path),
        "--cookie-file",
        str(cookie_path),
        "--job-id",
        job_id,
        "--timeout",
        str(timeout_s),
    ]
    popen_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_options)
    except OSError as exc:
        message = f"Failed to start QR login worker: {exc}"
        _write_state(
            state_path,
            {
                **_read_state(state_path),
                "status": "failed",
                "updated_at": time.time(),
                "message": message,
            },
        )
        raise RuntimeError(message) from exc

    deadline = time.monotonic() + startup_timeout_s
    while time.monotonic() < deadline:
        state = _read_state(state_path)
        if state.get("job_id") == job_id and state.get("status") in {
            "waiting",
            "scanned",
            "confirmed",
            *_TERMINAL_STATUSES,
        }:
            return state
        if process.poll() is not None:
            break
        time.sleep(0.1)

    state = _read_state(state_path)
    if state.get("job_id") == job_id and state.get("status") in {
        "waiting",
        "scanned",
        "confirmed",
        *_TERMINAL_STATUSES,
    }:
        return state

    _terminate_process(process)
    message = f"QR login worker did not produce a QR code within {startup_timeout_s:g} seconds."
    failed = {
        **state,
        "job_id": job_id,
        "status": "failed",
        "updated_at": time.time(),
        "message": message,
    }
    if _read_state(state_path).get("job_id") == job_id:
        _write_state(state_path, failed)
    raise RuntimeError(message)


def _run_worker(*, state_file: Path, cookie_file: Path, job_id: str, timeout_s: int) -> int:
    """Run one QR login job and persist status for later CLI invocations."""
    set_cookie_path(cookie_file)
    started_at = time.time()
    base_state = {
        "job_id": job_id,
        "pid": os.getpid(),
        "started_at": started_at,
        "expires_at": started_at + timeout_s + _WORKER_EXPIRY_GRACE_S,
        "cookie_file": str(cookie_file),
    }

    qr_state: dict[str, str] = {}

    def update(status: str, **fields: Any) -> None:
        current = _read_state(state_file)
        if current.get("job_id") not in {job_id, None}:
            return
        if current.get("job_id") == job_id and current.get("status") in _TERMINAL_STATUSES:
            return
        _write_state(
            state_file,
            {**base_state, **qr_state, "status": status, "updated_at": time.time(), **fields},
        )

    def on_qr(qr_url: str) -> None:
        qr_state["qr_url"] = qr_url
        update("waiting", message="Waiting for QR scan and confirmation")

    def on_status(message: str) -> None:
        if "Scanned!" in message:
            update("scanned", message="QR scanned; waiting for confirmation")
        elif "Login confirmed!" in message:
            update("confirmed", message="Login confirmed; saving session")

    update("starting", message="Starting browser-assisted QR login")
    try:
        qrcode_login(
            on_status=on_status,
            on_qr=on_qr,
            render_qr=False,
            timeout_s=timeout_s,
            prefer_browser_assisted=True,
        )
    except Exception as exc:
        update("failed", message=str(exc))
        return 1

    update("succeeded", message="QR login completed and cookies were saved")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--cookie-file", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if not args.worker:
        parser.error("This module is an internal QR login worker")
    return _run_worker(
        state_file=args.state_file,
        cookie_file=args.cookie_file,
        job_id=args.job_id,
        timeout_s=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
