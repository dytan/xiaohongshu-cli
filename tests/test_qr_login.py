"""Unit tests for QR code login flow."""

import pytest

from xhs_cli.command_normalizers import normalize_xhs_user_payload
from xhs_cli.exceptions import XhsApiError
from xhs_cli.qr_login import (
    BrowserQrLoginUnavailable,
    _browser_assisted_qrcode_login,
    _display_login_qr,
    _ensure_camoufox_ready,
    _normalize_browser_cookies,
    _render_qr_half_blocks,
    qrcode_login,
)


class _FakeQrClient:
    instances = []

    def __init__(self, cookies, request_delay=0, **kwargs):
        self.cookies = dict(cookies)
        self.activate_calls = 0
        self.status_calls = 0
        self.complete_calls = 0
        self.create_seen_web_session = None
        self.status_seen_web_session = None
        self.complete_seen_web_sessions = []
        self.self_info_calls = 0
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login_activate(self):
        self.activate_calls += 1
        if self.activate_calls == 1:
            return {"session": "guest-session", "secure_session": "guest-sec", "user_id": "guest-user"}
        return {"session": "unexpected-session", "secure_session": "unexpected-sec", "user_id": "unexpected-user"}

    def create_qr_login(self):
        self.create_seen_web_session = self.cookies.get("web_session")
        return {"qr_id": "qr-1", "code": "code-1", "url": "https://example.com/qr"}

    def check_qr_status(self, qr_id, code):
        self.status_calls += 1
        self.status_seen_web_session = self.cookies.get("web_session")
        return {"codeStatus": 2, "userId": "real-user"}

    def complete_qr_login(self, qr_id, code):
        self.complete_calls += 1
        self.complete_seen_web_sessions.append(self.cookies.get("web_session"))
        self.cookies["web_session"] = "real-session"
        self.cookies["web_session_sec"] = "real-sec"
        return {
            "code_status": 2,
            "login_info": {
                "user_id": "real-user",
                "session": "real-session",
                "secure_session": "real-sec",
            },
        }

    def get_self_info(self):
        self.self_info_calls += 1
        return {
            "user_id": "real-user",
            "basic_info": {
                "user_id": "real-user",
                "nickname": "Alice",
                "red_id": "alice001",
            },
        }


class _MismatchQrClient(_FakeQrClient):
    def complete_qr_login(self, qr_id, code):
        self.complete_calls += 1
        self.complete_seen_web_sessions.append(self.cookies.get("web_session"))
        self.cookies["web_session"] = "wrong-session"
        self.cookies["web_session_sec"] = "wrong-sec"
        return {
            "code_status": 2,
            "login_info": {
                "user_id": "guest-user",
                "session": "wrong-session",
                "secure_session": "wrong-sec",
            },
        }

    def get_self_info(self):
        self.self_info_calls += 1
        return {
            "user_id": "guest-user",
            "basic_info": {
                "user_id": "guest-user",
                "nickname": "Guest",
                "red_id": "",
            },
        }


class _SelfInfoFallbackQrClient(_FakeQrClient):
    def complete_qr_login(self, qr_id, code):
        self.complete_calls += 1
        self.complete_seen_web_sessions.append(self.cookies.get("web_session"))
        self.cookies["web_session"] = "real-session"
        self.cookies["web_session_sec"] = "real-sec"
        return {
            "code_status": 2,
            "login_info": {
                "user_id": "guest-user",
                "session": "real-session",
                "secure_session": "real-sec",
            },
        }


def test_qrcode_login_completes_after_confirmation_and_saves_real_session(monkeypatch):
    saved = []

    monkeypatch.setattr("xhs_cli.qr_login.XhsClient", _FakeQrClient)
    monkeypatch.setattr("xhs_cli.qr_login._generate_a1", lambda: "a1-fixed")
    monkeypatch.setattr("xhs_cli.qr_login._generate_webid", lambda: "webid-fixed")
    monkeypatch.setattr("xhs_cli.qr_login._display_qr_in_terminal", lambda data: True)
    monkeypatch.setattr("xhs_cli.qr_login.time.sleep", lambda seconds: None)
    monkeypatch.setattr("xhs_cli.qr_login.save_cookies", lambda cookies: saved.append(cookies))

    cookies = qrcode_login(timeout_s=1)
    client = _FakeQrClient.instances[-1]

    assert client.create_seen_web_session == "guest-session"
    assert client.status_seen_web_session == "guest-session"
    assert client.activate_calls == 1
    assert client.complete_calls == 1
    assert client.complete_seen_web_sessions == ["guest-session"]
    assert cookies == {
        "a1": "a1-fixed",
        "webId": "webid-fixed",
        "web_session": "real-session",
        "web_session_sec": "real-sec",
    }
    assert saved == [cookies]


def test_qrcode_login_accepts_confirmed_user_from_self_info_fallback(monkeypatch):
    saved = []

    monkeypatch.setattr("xhs_cli.qr_login.XhsClient", _SelfInfoFallbackQrClient)
    monkeypatch.setattr("xhs_cli.qr_login._generate_a1", lambda: "a1-fixed")
    monkeypatch.setattr("xhs_cli.qr_login._generate_webid", lambda: "webid-fixed")
    monkeypatch.setattr("xhs_cli.qr_login._display_qr_in_terminal", lambda data: True)
    monkeypatch.setattr("xhs_cli.qr_login.time.sleep", lambda seconds: None)
    monkeypatch.setattr("xhs_cli.qr_login.save_cookies", lambda cookies: saved.append(cookies))

    cookies = qrcode_login(timeout_s=1)
    client = _SelfInfoFallbackQrClient.instances[-1]

    assert client.activate_calls == 1
    assert client.complete_calls == 1
    assert client.self_info_calls >= 1
    assert cookies == {
        "a1": "a1-fixed",
        "webId": "webid-fixed",
        "web_session": "real-session",
        "web_session_sec": "real-sec",
    }
    assert saved == [cookies]


def test_qrcode_login_rejects_mismatched_confirmed_user(monkeypatch):
    monkeypatch.setattr("xhs_cli.qr_login.XhsClient", _MismatchQrClient)
    monkeypatch.setattr("xhs_cli.qr_login._generate_a1", lambda: "a1-fixed")
    monkeypatch.setattr("xhs_cli.qr_login._generate_webid", lambda: "webid-fixed")
    monkeypatch.setattr("xhs_cli.qr_login._display_qr_in_terminal", lambda data: True)
    monkeypatch.setattr("xhs_cli.qr_login.time.sleep", lambda seconds: None)
    monkeypatch.setattr("xhs_cli.qr_login.save_cookies", lambda cookies: None)

    with pytest.raises(XhsApiError, match="completion never returned"):
        qrcode_login(timeout_s=1)


def test_render_qr_half_blocks_can_match_dark_or_light_backgrounds():
    matrix = [[True, False], [False, True]]

    assert _render_qr_half_blocks(matrix) == "▀▄"
    assert _render_qr_half_blocks(matrix, invert=True) == "▄▀"


def test_display_qr_prints_variants_for_dark_and_light_backgrounds(capsys):
    from xhs_cli.qr_login import _display_qr_in_terminal

    assert _display_qr_in_terminal("https://example.com/temporary-qr") is True

    output = capsys.readouterr().out
    assert "Dark-background QR" in output
    assert "Light-background QR" in output


def test_display_login_qr_always_emits_temporary_url(monkeypatch):
    messages = []
    monkeypatch.setattr("xhs_cli.qr_login._display_qr_in_terminal", lambda data: True)

    _display_login_qr("https://example.com/temporary-qr", messages.append)

    assert "QR URL: https://example.com/temporary-qr" in messages


def test_display_login_qr_keeps_url_when_rendering_fails(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "xhs_cli.qr_login._display_qr_in_terminal",
        lambda data: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    _display_login_qr("https://example.com/temporary-qr", messages.append)

    assert messages[0] == "QR URL: https://example.com/temporary-qr"
    assert any("Unable to render QR" in message for message in messages)


def test_ensure_camoufox_ready_never_downloads(monkeypatch, tmp_path):
    browser_dir = tmp_path / "camoufox"
    browser_dir.mkdir()
    executable = browser_dir / "camoufox-bin"
    executable.write_text("")
    executable.chmod(0o700)
    calls = []

    monkeypatch.setattr(
        "camoufox.pkgman.camoufox_path",
        lambda download_if_missing: calls.append(download_if_missing) or browser_dir,
    )
    monkeypatch.setattr("camoufox.pkgman.launch_path", lambda: str(executable))

    _ensure_camoufox_ready()

    assert calls == [False]


def test_browser_assisted_qrcode_login_uses_headless_camoufox(monkeypatch):
    launch_options = []

    class FakeResponse:
        request = type("Request", (), {"method": "GET"})()

        def __init__(self, data):
            self.url = "https://www.xiaohongshu.com/api/sns/web/v1/login/qrcode/status"
            self.data = data

        def json(self):
            return {"success": True, "data": self.data}

    class FakeResponseInfo:
        def __init__(self, data):
            self.value = FakeResponse(data)

    class FakeExpectation:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return FakeResponseInfo(self.data)

        def __exit__(self, *args):
            return False

    class FakeContext:
        def cookies(self):
            return [
                {"name": "a1", "value": "a1-1", "domain": ".xiaohongshu.com"},
                {"name": "webId", "value": "webid-1", "domain": ".xiaohongshu.com"},
            ]

    class FakePage:
        context = FakeContext()

        def __init__(self):
            self.expectations = 0

        def on(self, *args):
            pass

        def expect_response(self, predicate, timeout):
            self.expectations += 1
            if self.expectations == 1:
                return FakeExpectation({"url": "https://example.com/temporary-qr"})
            return FakeExpectation({
                "code_status": 2,
                "login_info": {
                    "user_id": "user-1",
                    "session": "session-1",
                    "secure_session": "secure-1",
                },
            })

        def goto(self, *args, **kwargs):
            pass

    class FakeBrowser:
        def new_page(self):
            return FakePage()

    class FakeCamoufox:
        def __init__(self, **kwargs):
            launch_options.append(kwargs)

        def __enter__(self):
            return FakeBrowser()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("xhs_cli.qr_login._ensure_camoufox_ready", lambda: None)
    monkeypatch.setattr("xhs_cli.qr_login._display_login_qr", lambda *args: None)
    monkeypatch.setattr("xhs_cli.qr_login._wait_for_browser_login_settled", lambda page: None)
    monkeypatch.setattr("xhs_cli.qr_login.save_cookies", lambda cookies: None)
    monkeypatch.setattr("camoufox.sync_api.Camoufox", FakeCamoufox)

    cookies = _browser_assisted_qrcode_login(timeout_s=1)

    assert launch_options[0]["headless"] is True
    assert [addon.name for addon in launch_options[0]["exclude_addons"]] == ["UBO"]
    assert cookies["web_session"] == "session-1"


def test_qrcode_login_prefers_browser_assisted_backend(monkeypatch):
    saved = []

    monkeypatch.setattr(
        "xhs_cli.qr_login._browser_assisted_qrcode_login",
        lambda **kwargs: {
            "a1": "a1-browser",
            "webId": "webid-browser",
            "web_session": "0400-browser",
            "web_session_sec": "secure-browser",
            "id_token": "token-browser",
        },
    )

    cookies = qrcode_login(timeout_s=1, prefer_browser_assisted=True)

    assert cookies["web_session"] == "0400-browser"
    assert cookies["id_token"] == "token-browser"
    assert saved == []


def test_qrcode_login_falls_back_when_browser_backend_unavailable(monkeypatch):
    monkeypatch.setattr(
        "xhs_cli.qr_login._browser_assisted_qrcode_login",
        lambda **kwargs: (_ for _ in ()).throw(BrowserQrLoginUnavailable("missing camoufox")),
    )
    monkeypatch.setattr(
        "xhs_cli.qr_login._http_qrcode_login",
        lambda **kwargs: {
            "a1": "a1-http",
            "webId": "webid-http",
            "web_session": "http-session",
        },
    )

    cookies = qrcode_login(timeout_s=1, prefer_browser_assisted=True)

    assert cookies == {
        "a1": "a1-http",
        "webId": "webid-http",
        "web_session": "http-session",
    }


def test_normalize_browser_cookies_uses_allowlist():
    cookies = _normalize_browser_cookies([
        {"name": "a1", "value": "a1-value", "domain": ".xiaohongshu.com"},
        {"name": "web_session", "value": "session-value", "domain": ".xiaohongshu.com"},
        {"name": "customer-sso-sid", "value": "skip-me", "domain": ".xiaohongshu.com"},
        {"name": "creator_only", "value": "skip-me-too", "domain": "creator.xiaohongshu.com"},
    ])

    assert cookies == {
        "a1": "a1-value",
        "web_session": "session-value",
    }


def test_normalize_xhs_user_payload_reads_basic_info():
    user = normalize_xhs_user_payload({
        "guest": False,
        "basic_info": {
            "user_id": "user-1",
            "nickname": "Alice",
            "red_id": "alice001",
            "ip_location": "上海",
            "desc": "hello",
        },
    })

    assert user == {
        "id": "user-1",
        "name": "Alice",
        "username": "alice001",
        "nickname": "Alice",
        "red_id": "alice001",
        "ip_location": "上海",
        "desc": "hello",
        "guest": False,
    }
