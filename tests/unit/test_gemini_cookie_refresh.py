from unittest.mock import AsyncMock

import pytest

from app.core import gemini_client as client_module
from app.core.fingerprint import cookie_jar as cookie_jar_module
from app.core.fingerprint.cookie_jar import PersistentCookieJar
from app.core.gemini_client import GEMINI_APP_EN_URL, GeminiWebClient


class FakeCookieJar:
    def __init__(self):
        self.cookies = {
            "__Secure-1PSID": "psid",
            "__Secure-1PSIDTS": "psidts",
            "GOOGLE_ABUSE_EXEMPTION": "stale",
            "NID": "stale",
        }

    def cookie_names(self):
        return list(self.cookies)

    def remove(self, name):
        self.cookies.pop(name, None)

    def get_all(self):
        return dict(self.cookies)

    def update_from_response(self, response):
        pass


class FakeSession:
    def __init__(self, text='"SNlM0e":"session-token"'):
        self.cookies = self
        self.calls = []
        self.text = text

    def clear(self):
        pass

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return type("Response", (), {
            "status_code": 200,
            "text": self.text,
        })()


@pytest.mark.asyncio
async def test_token_refresh_discards_stale_auxiliary_cookies(monkeypatch):
    client = GeminiWebClient("psid", "psidts")
    client._cookie_jar = FakeCookieJar()
    client._http = FakeSession()
    client._ensure_session_current = AsyncMock()
    client._get_headers = lambda method: {}
    monkeypatch.setattr(client_module, "apply_jitter", AsyncMock())

    await client._obtain_session_token()

    assert client._session_token == "session-token"
    assert client._cookie_jar.cookie_names() == ["__Secure-1PSID", "__Secure-1PSIDTS"]
    assert client._http.calls == [(GEMINI_APP_EN_URL, {
        "cookies": {"__Secure-1PSID": "psid", "__Secure-1PSIDTS": "psidts"},
        "headers": {},
    })]


@pytest.mark.asyncio
async def test_invalid_cookie_triggers_bound_browser_profile():
    refresh = AsyncMock(return_value=True)
    client = GeminiWebClient("psid", "psidts", browser_refresh=refresh)
    client._cookie_jar = FakeCookieJar()
    client._http = FakeSession("signed out")
    client._ensure_session_current = AsyncMock()
    client._get_headers = lambda method: {}

    result = await client.check_account()

    assert result["valid"] is True
    assert result["browser_refreshed"] is True
    refresh.assert_awaited_once()


def test_response_cookie_update_ignores_content_push_duplicate_nid(tmp_path, monkeypatch):
    from curl_cffi.requests import Cookies

    monkeypatch.setattr(cookie_jar_module, "COOKIE_STORE_DIR", tmp_path)
    cookies = Cookies()
    cookies.set("NID", "google-nid", domain=".google.com")
    cookies.set("NID", "upload-nid", domain="content-push.googleapis.com")
    response = type("Response", (), {"cookies": cookies, "headers": {}})()
    jar = PersistentCookieJar("account-a")

    jar.update_from_response(response)

    assert jar.get("NID") == "google-nid"


@pytest.mark.asyncio
async def test_attachment_upload_cache_reuses_complete_upload(monkeypatch):
    upload = AsyncMock(return_value=[("file-1", "one.png"), ("file-2", "two.png")])
    monkeypatch.setattr("app.core.file_upload.upload_files", upload)
    client = GeminiWebClient.__new__(GeminiWebClient)
    client._http = object()
    client._push_id = "push-id"
    attachments = [{"filename": "one.png"}, {"filename": "two.png"}]
    cache = {}

    first = await client._upload_attachments(attachments, {}, {}, cache)
    second = await client._upload_attachments(attachments, {}, {}, cache)

    assert first == second
    assert cache["file_ids"] == first
    assert cache["duration_ms"] >= 0
    upload.assert_awaited_once()
