import asyncio
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
            "proxy_node_id": 12,
            "proxy_node_name": "Tokyo",
            "proxy_endpoint": "http://mihomo-gateway:19012",
            "route_fingerprint": "a" * 64,
        })
    finally:
        await service.aclose()

    assert result == {"success": True, "account_id": "flow-7", "flow_token_id": 7}
    assert pool.received == (7, {
        "psid": "psid",
        "psidts": "psidts",
        "email": "user@example.com",
        "name": "User",
        "proxy_node_id": 12,
        "proxy_node_name": "Tokyo",
        "proxy_url": "http://mihomo-gateway:19012",
        "route_fingerprint": "a" * 64,
    })


@pytest.mark.asyncio
async def test_flow_callback_rejects_cookies_without_the_fixed_proxy_route():
    service = FlowBridgeService(
        FakeAccountPool(),
        enabled=True,
        base_url="http://flow.example",
        secret="bridge-secret",
        timeout=5,
    )
    try:
        with pytest.raises(FlowBridgeError, match="fixed proxy route"):
            await service.accept_cookie_callback({
                "flow_token_id": 7,
                "__Secure-1PSID": "psid",
                "__Secure-1PSIDTS": "psidts",
            })
    finally:
        await service.aclose()


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


def test_gemini_http_sessions_use_the_flow_account_proxy(monkeypatch):
    created = []

    def fake_session(**options):
        created.append(options)
        return SimpleNamespace()

    monkeypatch.setattr("app.core.gemini_client.AsyncSession", fake_session)
    client = GeminiWebClient("psid", "psidts", proxy_url="http://mihomo-gateway:19012")
    client._current_target = "chrome124"

    client._new_http_session(timeout=180)

    assert created == [{
        "impersonate": "chrome124",
        "timeout": 180,
        "proxy": "http://mihomo-gateway:19012",
    }]


@pytest.mark.asyncio
async def test_flow_refresh_does_not_retry_terminal_client_errors(monkeypatch):
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
    sleep = AsyncMock()
    monkeypatch.setattr("app.core.flow_bridge.asyncio.sleep", sleep)
    try:
        with pytest.raises(FlowBridgeError):
            await service.refresh_token(9)
    finally:
        await service.aclose()

    assert service._request.await_count == 1
    sleep.assert_not_awaited()
    notifier.assert_awaited_once()
    account, error = notifier.await_args.args
    assert account.id == "flow-9"
    assert error == "login required"


@pytest.mark.asyncio
async def test_flow_refresh_retries_transient_server_errors(monkeypatch):
    service = FlowBridgeService(
        FakeAccountPool(),
        enabled=True,
        base_url="http://flow.example",
        secret="bridge-secret",
        timeout=5,
    )
    service._request = AsyncMock(side_effect=FlowBridgeError("flow_unavailable", "unavailable", 503))
    sleep = AsyncMock()
    monkeypatch.setattr("app.core.flow_bridge.asyncio.sleep", sleep)
    monkeypatch.setattr("app.core.flow_bridge.random.uniform", lambda _minimum, _maximum: 2.0)
    try:
        with pytest.raises(FlowBridgeError):
            await service.refresh_token(9)
    finally:
        await service.aclose()

    assert service._request.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_flow_business_disable_does_not_disable_gemini_account(monkeypatch):
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
    monkeypatch.setattr("app.core.flow_bridge.asyncio.sleep", AsyncMock())
    try:
        with pytest.raises(FlowBridgeError, match="disabled"):
            await service.refresh_token(18)
    finally:
        await service.aclose()

    assert account.status == AccountStatus.ACTIVE
    notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_accounts_only_refreshes_selected_ready_profiles():
    service = FlowBridgeService(
        FakeAccountPool(),
        enabled=True,
        base_url="http://flow.example",
        secret="bridge-secret",
    )
    service.list_accounts = AsyncMock(return_value=[
        {"flow_token_id": 7, "flow_enabled": False, "auth_ready": True, "profile_id": "profile-7"},
        {"flow_token_id": 8, "flow_enabled": True, "auth_ready": True, "profile_id": "profile-8"},
    ])
    service.refresh_token = AsyncMock(return_value={"success": True, "flow_token_id": 7})
    try:
        result = await service.sync_accounts([7])
    finally:
        await service.aclose()

    service.refresh_token.assert_awaited_once_with(7)
    assert result["available"] == 1
    assert result["refreshed"] == 1


@pytest.mark.asyncio
async def test_flow_account_list_marks_existing_mappings_as_synced():
    pool = FakeAccountPool()
    pool.accounts[7] = SimpleNamespace(id="flow-7")
    service = FlowBridgeService(pool, enabled=True, base_url="http://flow.example", secret="bridge-secret")
    service._request = AsyncMock(return_value={"accounts": [
        {"flow_token_id": 7},
        {"flow_token_id": 8},
    ]})
    try:
        accounts = await service.list_accounts()
    finally:
        await service.aclose()

    assert [account["synced"] for account in accounts] == [True, False]


@pytest.mark.asyncio
async def test_flow_sync_refreshes_up_to_four_profiles_concurrently():
    service = FlowBridgeService(FakeAccountPool(), enabled=True, base_url="http://flow.example", secret="bridge-secret")
    service.list_accounts = AsyncMock(return_value=[
        {"flow_token_id": token_id, "gemini_sync_ready": True}
        for token_id in range(1, 6)
    ])
    active = 0
    peak = 0

    async def refresh(token_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"success": True, "flow_token_id": token_id}

    service.refresh_token = refresh
    try:
        result = await service.sync_accounts()
    finally:
        await service.aclose()

    assert peak == 4
    assert result["refreshed"] == 5
