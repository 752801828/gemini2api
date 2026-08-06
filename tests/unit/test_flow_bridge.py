from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.core.account_pool import AccountStatus
from app.core.flow_bridge import FlowBridgeError, FlowBridgeService
from app.core.gemini_client import GeminiWebClient


class FakeAccountPool:
    def __init__(self):
        self.accounts = {}
        self.received = None

    async def upsert_flow_account(self, token_id, **cookies):
        self.received = (token_id, cookies)
        account = SimpleNamespace(id=f"flow-{token_id}", flow_token_id=token_id)
        self.accounts[token_id] = account
        return account

    def get_flow_account(self, token_id):
        return self.accounts.get(token_id)


@pytest.mark.asyncio
async def test_flow_callback_updates_only_the_flow_account_mapping():
    pool = FakeAccountPool()
    service = FlowBridgeService(
        pool,
        enabled=True,
        base_url="http://flow.example",
        secret="bridge-secret",
        timeout=5,
    )
    try:
        result = await service.accept_cookie_callback({
            "flow_token_id": 7,
            "email": "user@example.com",
            "name": "User",
            "__Secure-1PSID": "psid",
            "__Secure-1PSIDTS": "psidts",
        })
    finally:
        await service.aclose()

    assert result == {"success": True, "account_id": "flow-7", "flow_token_id": 7}
    assert pool.received == (7, {
        "psid": "psid",
        "psidts": "psidts",
        "email": "user@example.com",
        "name": "User",
    })


@pytest.mark.asyncio
async def test_empty_gemini_response_retries_before_returning(monkeypatch):
    client = GeminiWebClient("psid", "psidts")
    client._healthy = True
    client._send_request = AsyncMock(side_effect=[
        {"text": "", "images": []},
        {"text": "Service is running normally", "images": []},
    ])
    monkeypatch.setattr(settings, "max_retries", 3)
    monkeypatch.setattr("app.core.gemini_client.asyncio.sleep", AsyncMock())

    result = await client.generate("translate", "gemini-flash-lite")

    assert result["text"] == "Service is running normally"
    assert client._send_request.await_count == 2


@pytest.mark.asyncio
async def test_flow_refresh_failure_notifies_maintenance():
    pool = FakeAccountPool()
    service = FlowBridgeService(
        pool,
        enabled=True,
        base_url="http://flow.example",
        secret="bridge-secret",
        timeout=5,
    )
    notifier = AsyncMock(return_value={"sent": True})
    service.set_failure_notifier(notifier)
    service._request = AsyncMock(side_effect=FlowBridgeError("profile_not_ready", "login required", 409))
    try:
        with pytest.raises(FlowBridgeError):
            await service.refresh_token(9)
    finally:
        await service.aclose()

    notifier.assert_awaited_once()
    account, error = notifier.await_args.args
    assert account.id == "flow-9"
    assert error == "login required"


@pytest.mark.asyncio
async def test_disabled_flow_account_is_marked_disabled_without_flow_alert():
    pool = FakeAccountPool()
    account = SimpleNamespace(
        id="flow-18",
        label="Brady Auclair",
        source="flow",
        flow_token_id=18,
        flow_email="brady@example.com",
        status=AccountStatus.ACTIVE,
        last_error="",
    )
    pool.accounts[18] = account
    service = FlowBridgeService(pool, enabled=True, base_url="http://flow.example", secret="bridge-secret")
    notifier = AsyncMock(return_value={"sent": True})
    service.set_failure_notifier(notifier)
    service._request = AsyncMock(side_effect=FlowBridgeError("account_disabled", "Flow account is disabled", 409))
    try:
        with pytest.raises(FlowBridgeError, match="disabled"):
            await service.refresh_token(18)
    finally:
        await service.aclose()

    assert account.status == AccountStatus.DISABLED
    notifier.assert_not_awaited()
