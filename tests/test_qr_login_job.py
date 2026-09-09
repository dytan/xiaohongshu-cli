"""Tests for detached QR login jobs."""

import json
import time
from pathlib import Path

import pytest

from xhs_cli.cookies import save_cookies
from xhs_cli.qr_login_job import (
    _run_worker,
    _write_state,
    get_qr_login_state_path,
    get_qr_login_status,
    start_qr_login,
)


def test_start_qr_login_detaches_and_returns_ready_state(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    cookie_file = tmp_path / "cookies.json"
    popen_calls = []

    class FakeProcess:
        pid = 1234

        @staticmethod
        def poll():
            return None

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        job_id = command[command.index("--job-id") + 1]
        state_file.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "pid": 1234,
                    "status": "waiting",
                    "qr_url": "https://example.com/qr",
                    "expires_at": time.time() + 60,
                }
            )
        )
        return FakeProcess()

    monkeypatch.setattr("xhs_cli.qr_login_job.subprocess.Popen", fake_popen)
    monkeypatch.setattr("xhs_cli.qr_login_job._process_is_running", lambda pid: pid == 1234)

    state = start_qr_login(
        state_file=state_file,
        cookie_file=cookie_file,
        startup_timeout_s=0.1,
    )

    assert state["status"] == "waiting"
    assert state["qr_url"] == "https://example.com/qr"
    command, kwargs = popen_calls[0]
    assert command[command.index("--cookie-file") + 1] == str(cookie_file)
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None
    assert kwargs.get("start_new_session") is True


def test_worker_publishes_qr_then_success(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    cookie_file = tmp_path / "cookies.json"
    seen = {}

    def fake_login(**kwargs):
        kwargs["on_qr"]("https://example.com/qr")
        seen["waiting"] = get_qr_login_status(state_file=state_file)
        return {"a1": "fake", "web_session": "fake-session"}

    monkeypatch.setattr("xhs_cli.qr_login_job.qrcode_login", fake_login)

    assert _run_worker(
        state_file=state_file,
        cookie_file=cookie_file,
        job_id="job-1",
        timeout_s=1,
    ) == 0
    assert seen["waiting"]["status"] == "waiting"
    assert seen["waiting"]["qr_url"] == "https://example.com/qr"
    succeeded = get_qr_login_status(state_file=state_file)
    assert succeeded["status"] == "succeeded"
    assert succeeded["qr_url"] == "https://example.com/qr"


def test_worker_records_failure_without_raising(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"

    def fake_login(**kwargs):
        raise RuntimeError("browser stopped")

    monkeypatch.setattr("xhs_cli.qr_login_job.qrcode_login", fake_login)

    assert _run_worker(
        state_file=state_file,
        cookie_file=Path(tmp_path / "cookies.json"),
        job_id="job-1",
        timeout_s=1,
    ) == 1
    state = get_qr_login_status(state_file=state_file)
    assert state["status"] == "failed"
    assert state["message"] == "browser stopped"


def test_worker_does_not_revive_terminal_job(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    _write_state(state_file, {"job_id": "job-1", "status": "failed", "message": "expired"})

    def fake_login(**kwargs):
        kwargs["on_qr"]("https://example.com/qr")

    monkeypatch.setattr("xhs_cli.qr_login_job.qrcode_login", fake_login)

    assert _run_worker(
        state_file=state_file,
        cookie_file=tmp_path / "cookies.json",
        job_id="job-1",
        timeout_s=1,
    ) == 0
    assert get_qr_login_status(state_file=state_file)["status"] == "failed"


def test_worker_keeps_qr_url_when_login_fails(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"

    def fake_login(**kwargs):
        kwargs["on_qr"]("https://example.com/qr")
        raise RuntimeError("browser stopped")

    monkeypatch.setattr("xhs_cli.qr_login_job.qrcode_login", fake_login)

    assert _run_worker(
        state_file=state_file,
        cookie_file=tmp_path / "cookies.json",
        job_id="job-1",
        timeout_s=1,
    ) == 1
    state = get_qr_login_status(state_file=state_file)
    assert state["status"] == "failed"
    assert state["qr_url"] == "https://example.com/qr"


def test_worker_saves_cookies_to_custom_path(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    cookie_file = tmp_path / "persistent" / "cookies.json"

    def fake_login(**kwargs):
        kwargs["on_qr"]("https://example.com/qr")
        save_cookies({"a1": "fake", "web_session": "fake-session"})

    monkeypatch.setattr("xhs_cli.qr_login_job.qrcode_login", fake_login)

    assert _run_worker(
        state_file=state_file,
        cookie_file=cookie_file,
        job_id="job-1",
        timeout_s=1,
    ) == 0
    assert json.loads(cookie_file.read_text())["a1"] == "fake"


def test_status_marks_dead_worker_failed_and_preserves_qr(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    _write_state(
        state_file,
        {
            "job_id": "job-1",
            "pid": 1234,
            "status": "waiting",
            "qr_url": "https://example.com/qr",
            "expires_at": time.time() + 60,
        },
    )
    monkeypatch.setattr("xhs_cli.qr_login_job._process_is_running", lambda pid: False)

    state = get_qr_login_status(state_file=state_file)

    assert state["status"] == "failed"
    assert state["qr_url"] == "https://example.com/qr"
    assert "stopped" in state["message"]
    assert json.loads(state_file.read_text())["status"] == "failed"


def test_status_marks_expired_worker_failed_even_if_pid_is_live(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    _write_state(
        state_file,
        {
            "job_id": "job-1",
            "pid": 1234,
            "status": "waiting",
            "expires_at": time.time() - 1,
        },
    )
    monkeypatch.setattr("xhs_cli.qr_login_job._process_is_running", lambda pid: True)

    state = get_qr_login_status(state_file=state_file)

    assert state["status"] == "failed"
    assert "expired" in state["message"]


def test_state_file_has_private_permissions(tmp_path):
    state_file = tmp_path / "qr_login.json"

    _write_state(state_file, {"status": "waiting"})

    assert state_file.stat().st_mode & 0o777 == 0o600


def test_config_dir_controls_qr_state_path(tmp_path, monkeypatch):
    config_dir = tmp_path / "persistent" / "xhs"
    monkeypatch.setenv("XHS_CONFIG_DIR", str(config_dir))

    assert get_qr_login_state_path() == config_dir / "qr_login.json"


def test_active_job_is_rejected(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    _write_state(
        state_file,
        {
            "job_id": "job-1",
            "pid": 1234,
            "status": "waiting",
            "expires_at": time.time() + 60,
        },
    )
    monkeypatch.setattr("xhs_cli.qr_login_job._process_is_running", lambda pid: True)

    with pytest.raises(RuntimeError, match="already in progress"):
        start_qr_login(state_file=state_file, cookie_file=tmp_path / "cookies.json")


def test_stale_job_allows_new_start(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    _write_state(
        state_file,
        {
            "job_id": "old-job",
            "pid": 111,
            "status": "waiting",
            "expires_at": time.time() + 60,
        },
    )

    class FakeProcess:
        pid = 222

        @staticmethod
        def poll():
            return None

    def fake_popen(command, **kwargs):
        job_id = command[command.index("--job-id") + 1]
        _write_state(
            state_file,
            {
                "job_id": job_id,
                "pid": 222,
                "status": "waiting",
                "qr_url": "https://example.com/new-qr",
                "expires_at": time.time() + 60,
            },
        )
        return FakeProcess()

    monkeypatch.setattr("xhs_cli.qr_login_job.subprocess.Popen", fake_popen)
    monkeypatch.setattr("xhs_cli.qr_login_job._process_is_running", lambda pid: pid == 222)

    state = start_qr_login(
        state_file=state_file,
        cookie_file=tmp_path / "cookies.json",
        startup_timeout_s=0.1,
    )

    assert state["status"] == "waiting"
    assert state["job_id"] != "old-job"


def test_spawn_failure_is_persisted(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"

    def fail_to_spawn(*args, **kwargs):
        raise OSError("process unavailable")

    monkeypatch.setattr("xhs_cli.qr_login_job.subprocess.Popen", fail_to_spawn)

    with pytest.raises(RuntimeError, match="Failed to start QR login worker"):
        start_qr_login(state_file=state_file, cookie_file=tmp_path / "cookies.json")

    state = get_qr_login_status(state_file=state_file)
    assert state["status"] == "failed"
    assert "process unavailable" in state["message"]


def test_startup_timeout_stops_spawned_worker(tmp_path, monkeypatch):
    state_file = tmp_path / "qr_login.json"
    stopped = []

    class FakeProcess:
        pid = 1234

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr("xhs_cli.qr_login_job.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr("xhs_cli.qr_login_job._terminate_process", lambda process: stopped.append(process.pid))

    with pytest.raises(RuntimeError, match="did not produce a QR code"):
        start_qr_login(
            state_file=state_file,
            cookie_file=tmp_path / "cookies.json",
            startup_timeout_s=0,
        )

    assert stopped == [1234]
    assert get_qr_login_status(state_file=state_file)["status"] == "failed"
