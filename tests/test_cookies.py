"""Unit tests for cookie management (no network required)."""


import time

import pytest

from xhs_cli.cookies import (
    NOTE_CONTEXT_TTL_SECONDS,
    cache_note_context,
    clear_cookies,
    cookies_to_string,
    get_cached_note_context,
    get_cached_xsec_token,
    get_config_dir,
    get_cookie_path,
    get_cookies,
    get_index_cache_path,
    get_note_by_index,
    get_token_cache_path,
    load_saved_cookies,
    load_token_cache,
    parse_cookie_string,
    save_cookies,
    save_note_index,
)


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Override config dir to use temp directory."""
    monkeypatch.setattr("xhs_cli.cookies.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("xhs_cli.cookies.get_cookie_path", lambda: tmp_path / "cookies.json")
    monkeypatch.setattr("xhs_cli.cookies._TOKEN_CACHE_MEMORY", None)
    monkeypatch.setattr("xhs_cli.cookies._TOKEN_CACHE_PATH", None)
    return tmp_path


class TestConfigDir:
    def test_environment_moves_default_files_and_creates_directory(
        self, tmp_path, monkeypatch
    ):
        config_dir = tmp_path / "persistent" / "xhs"
        monkeypatch.setenv("XHS_CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("xhs_cli.cookies._COOKIE_PATH", None)

        assert get_config_dir() == config_dir
        assert config_dir.is_dir()
        assert get_cookie_path() == config_dir / "cookies.json"
        assert get_token_cache_path() == config_dir / "token_cache.json"
        assert get_index_cache_path() == config_dir / "index_cache.json"

    def test_environment_expands_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XHS_CONFIG_DIR", "~/persistent/xhs")

        assert get_config_dir() == tmp_path / "persistent" / "xhs"


class TestSaveCookies:
    def test_save_and_load(self, tmp_config_dir):
        cookies = {"a1": "test_value", "web_session": "sess123"}
        save_cookies(cookies)

        loaded = load_saved_cookies()
        assert loaded is not None
        assert loaded["a1"] == "test_value"
        assert loaded["web_session"] == "sess123"

    def test_file_permissions(self, tmp_config_dir):
        cookies = {"a1": "test"}
        save_cookies(cookies)

        cookie_file = tmp_config_dir / "cookies.json"
        stat = cookie_file.stat()
        assert stat.st_mode & 0o777 == 0o600

    def test_custom_path_creates_parent(self, tmp_path, monkeypatch):
        cookie_file = tmp_path / "persistent" / "xhs" / "cookies.json"
        monkeypatch.setattr("xhs_cli.cookies._COOKIE_PATH", cookie_file)

        save_cookies({"a1": "test"})

        assert cookie_file.exists()
        assert load_saved_cookies()["a1"] == "test"

    def test_replaces_symlink_without_overwriting_its_target(self, tmp_path, monkeypatch):
        target = tmp_path / "target.json"
        target.write_text("do not overwrite")
        cookie_file = tmp_path / "cookies.json"
        cookie_file.symlink_to(target)
        monkeypatch.setattr("xhs_cli.cookies._COOKIE_PATH", cookie_file)

        save_cookies({"a1": "test"})

        assert not cookie_file.is_symlink()
        assert target.read_text() == "do not overwrite"
        assert load_saved_cookies()["a1"] == "test"


class TestParseCookieString:
    def test_preserves_equals_in_values(self):
        assert parse_cookie_string(" a1=fake== ; web_session=session+value= ") == {
            "a1": "fake==",
            "web_session": "session+value=",
        }

    def test_allows_empty_optional_cookie_values(self):
        assert parse_cookie_string("a1=fake; optional=") == {
            "a1": "fake",
            "optional": "",
        }

    @pytest.mark.parametrize(
        "raw",
        ["", "web_session=fake", "a1=fake; malformed", "a1=", "a1=fake; =value"],
    )
    def test_rejects_invalid_cookie_strings(self, raw):
        with pytest.raises(ValueError, match="Invalid cookie string"):
            parse_cookie_string(raw)


class TestLoadSavedCookies:
    def test_no_file(self, tmp_config_dir):
        assert load_saved_cookies() is None

    def test_invalid_json(self, tmp_config_dir):
        (tmp_config_dir / "cookies.json").write_text("not json")
        assert load_saved_cookies() is None

    def test_missing_a1(self, tmp_config_dir):
        (tmp_config_dir / "cookies.json").write_text('{"web_session": "x"}')
        assert load_saved_cookies() is None


class TestClearCookies:
    def test_clear_existing(self, tmp_config_dir):
        save_cookies({"a1": "test"})
        clear_cookies()
        assert load_saved_cookies() is None

    def test_clear_nonexistent(self, tmp_config_dir):
        # Should not raise
        clear_cookies()


class TestCookiesToString:
    def test_format(self):
        result = cookies_to_string({"a1": "v1", "web_session": "v2"})
        assert "a1=v1" in result
        assert "web_session=v2" in result
        assert "; " in result


class TestGetCookies:
    def test_prefers_saved_cookies_by_default(self, monkeypatch):
        monkeypatch.setattr("xhs_cli.cookies.load_saved_cookies", lambda: {"a1": "saved"})
        monkeypatch.setattr(
            "xhs_cli.cookies.extract_browser_cookies",
            lambda source: ("chrome", {"a1": "fresh"}),
        )

        browser, cookies = get_cookies("chrome")
        assert browser == "saved"
        assert cookies == {"a1": "saved"}

    def test_force_refresh_bypasses_saved_cookies(self, monkeypatch):
        monkeypatch.setattr("xhs_cli.cookies.load_saved_cookies", lambda: {"a1": "saved"})
        monkeypatch.setattr(
            "xhs_cli.cookies.extract_browser_cookies",
            lambda source: ("chrome", {"a1": "fresh"}),
        )
        saved = []
        monkeypatch.setattr("xhs_cli.cookies.save_cookies", lambda cookies: saved.append(cookies))

        browser, cookies = get_cookies("chrome", force_refresh=True)
        assert browser == "chrome"
        assert cookies == {"a1": "fresh"}
        assert saved == [{"a1": "fresh"}]


class TestNoteContextCache:
    def test_cache_persists_token_and_source(self, tmp_config_dir):
        cache_note_context("note-1", "token-1", "pc_search", context="search")

        assert get_cached_xsec_token("note-1") == "token-1"
        context = get_cached_note_context("note-1")
        assert context["token"] == "token-1"
        assert context["source"] == "pc_search"
        assert context["context"] == "search"

    def test_load_token_cache_keeps_legacy_entries_compatible(self, tmp_config_dir):
        get_token_cache_path().write_text('{"note-1":"legacy-token"}')

        cache = load_token_cache()
        assert cache["note-1"]["token"] == "legacy-token"
        assert cache["note-1"]["source"] == ""

    def test_expired_note_context_is_not_returned(self, tmp_config_dir):
        stale_ts = time.time() - NOTE_CONTEXT_TTL_SECONDS - 10
        get_token_cache_path().write_text(
            f'{{"note-1":{{"token":"stale-token","source":"pc_search","ts":{stale_ts}}}}}'
        )

        assert get_cached_note_context("note-1") == {}


class TestNoteIndexCache:
    def test_save_and_resolve_index_with_source(self, tmp_config_dir):
        save_note_index([
            {
                "note_id": "note-1",
                "xsec_token": "token-1",
                "xsec_source": "pc_search",
            }
        ])

        assert get_note_by_index(1) == {
            "note_id": "note-1",
            "xsec_token": "token-1",
            "xsec_source": "pc_search",
        }

    def test_save_empty_index_clears_previous_entries(self, tmp_config_dir):
        save_note_index([
            {
                "note_id": "note-1",
                "xsec_token": "token-1",
                "xsec_source": "pc_search",
            }
        ])
        save_note_index([])

        assert get_note_by_index(1) is None
        assert get_index_cache_path().read_text() == "[]"

    def test_index_file_permissions(self, tmp_config_dir):
        save_note_index([{"note_id": "note-1", "xsec_token": "", "xsec_source": ""}])

        stat = get_index_cache_path().stat()
        assert stat.st_mode & 0o777 == 0o600

    def test_index_normalizes_missing_optional_fields(self, tmp_config_dir):
        get_index_cache_path().write_text('[{"note_id":"note-1"}]')

        assert get_note_by_index(1) == {
            "note_id": "note-1",
            "xsec_token": "",
            "xsec_source": "",
        }
