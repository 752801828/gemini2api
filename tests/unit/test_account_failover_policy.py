import asyncio
import types

import pytest

from app.config import Settings, settings
from app.core.account_pool import AccountPool, HTTPStatusError, _is_retryable
from app.core.gemini_client import GeminiWebClient
from app.core.usage_metrics import live_metrics


class _FailClient:
    is_healthy = True

    def __init__(self, error):
        self.error = error
        self.calls = 0

    async def generate(self, *_args):
        self.calls += 1
        raise self.error


class _StreamRetryClient:
    is_healthy = True

    def __init__(self):
        self.calls = 0

    async def generate_stream(self, *_args):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("network timeout")
        yield {"type": "final", "text": "ok"}


def test_request_failover_refreshes_ck_and_stops_after_three_accounts():
    live_metrics.drain()
    pool = AccountPool()
    accounts = [
        types.SimpleNamespace(id=f"account-{index}", client=_FailClient(RuntimeError("network timeout")))
        for index in range(4)
    ]
    selected = []
    refreshed = []

    async def acquire(exclude=None):
        account = accounts[len(selected)]
        selected.append(account.id)
        return account

    async def release(_account, success, cooldown=False):
        return None

    async def refresh(account):
        refreshed.append(account.id)

    pool.acquire = acquire
    pool.release = release
    pool._refresh_failed_account = refresh

    with pytest.raises(RuntimeError, match="network timeout"):
        asyncio.run(pool.generate("hello", "gemini-pro"))

    assert selected == ["account-0", "account-1", "account-2"]
    assert refreshed == selected
    assert accounts[3].client.calls == 0
    assert _is_retryable(HTTPStatusError(429)) is True
    assert pool._request_count == 1
    assert live_metrics.drain()["model_requests"] == {"gemini-pro": 1}


def test_stream_retries_same_account_once_before_failover():
    live_metrics.drain()
    pool = AccountPool()
    client = _StreamRetryClient()
    account = types.SimpleNamespace(id="account-0", client=client)

    async def acquire(exclude=None):
        return account

    async def release(_account, success, cooldown=False):
        return None

    pool.acquire = acquire
    pool.release = release

    async def collect():
        return [event async for event in pool.generate_stream("hello", "gemini-pro")]

    assert asyncio.run(collect()) == [{"type": "final", "text": "ok"}]
    assert client.calls == 2
    assert pool._request_count == 1
    assert live_metrics.drain()["model_requests"] == {"gemini-pro": 1}


def test_non_stream_same_account_retry_is_hard_capped_at_one(monkeypatch):
    client = GeminiWebClient.__new__(GeminiWebClient)
    client._healthy = True
    client._family_model = {"pro": "gemini-3-pro"}
    calls = []

    async def send(*_args):
        calls.append(True)
        raise RuntimeError("network timeout")

    client._send_request = send
    monkeypatch.setattr(settings, "max_retries", 9)

    with pytest.raises(RuntimeError, match="Exhausted 2 attempts"):
        asyncio.run(client.generate("hello", "gemini-pro"))

    assert len(calls) == 2
    assert Settings(max_retries=9, _env_file=None).max_retries == 2
