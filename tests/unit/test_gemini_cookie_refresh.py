from unittest.mock import AsyncMock

import pytest

from app.core import gemini_client as client_module
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
    def __init__(self):
        self.cookies = self
        self.calls = []

    def clear(self):
        pass

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return type("Response", (), {
            "status_code": 200,
            "text": '"SNlM0e":"session-token"',
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
