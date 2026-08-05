import asyncio
import types
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch
from app.core.account_pool import Account, AccountPool, AccountStatus


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def list_gems(self):
        return [{"id": "g1", "name": "n", "description": "", "prompt": ""}]

    async def create_gem(self, name, prompt, description=""):
        return "new-id"

    async def update_gem(self, gem_id, name, prompt, description=""):
        self.calls.append(("update_gem", gem_id, name, prompt, description))
        return True

    async def delete_gem(self, gem_id):
        self.calls.append(("delete_gem", gem_id))
        return True

    async def generate(self, prompt, model, conversation_id="", attachments=None, gem_id=None):
        self.calls.append(("generate", prompt, model, gem_id))
        return {"text": "ok", "images": [], "conversation_id": "c1"}

    async def generate_stream(self, prompt, model, conversation_id="", attachments=None, gem_id=None):
        self.calls.append(("generate_stream", prompt, model, gem_id))
        yield {"type": "delta", "text": "hello"}
        yield {"type": "final", "text": "hello", "images": [], "conversation_id": "c2"}


def _pool_with_accounts():
    pool = AccountPool.__new__(AccountPool)
    a0 = types.SimpleNamespace(id="account-0", client=_FakeClient())
    a1 = types.SimpleNamespace(id="account-1", client=_FakeClient())
    pool._accounts = [a0, a1]
    return pool, a0, a1


def test_get_account_by_id():
    pool, a0, a1 = _pool_with_accounts()
    assert pool._get_account("account-1") is a1
    assert pool._get_account("nope") is None


def test_list_gems_routes_to_account():
    pool, a0, a1 = _pool_with_accounts()
    gems = asyncio.run(pool.list_gems("account-0"))
    assert gems[0]["id"] == "g1"


def test_list_gems_unknown_account_raises():
    pool, a0, a1 = _pool_with_accounts()
    with pytest.raises(ValueError):
        asyncio.run(pool.list_gems("ghost"))


def test_generate_pinned_account_passes_gem_id():
    """验证锁定逻辑：account_id 固定时，acquire 收到的 exclude 应包含其他所有账号 id。"""
    pool, a0, a1 = _pool_with_accounts()

    # 记录 acquire 被调用时的 exclude 参数
    received_excludes = []

    async def _fake_acquire(exclude=None):
        received_excludes.append(exclude)
        return a1

    async def _fake_release(account, success, cooldown=False):
        pass

    pool.acquire = _fake_acquire
    pool.release = _fake_release

    asyncio.run(pool.generate("hi", "gemini-pro", gem_id="g9", account_id="account-1"))

    # 1. 只命中绑定账号 account-1，且带上 gem_id
    assert a1.client.calls == [("generate", "hi", "gemini-pro", "g9")]
    assert a0.client.calls == []

    # 2. 验证锁定逻辑：acquire 收到的 exclude 应包含除 account-1 之外的所有账号（即 account-0）
    assert len(received_excludes) == 1
    assert received_excludes[0] == {"account-0"}


def test_generate_unknown_account_raises():
    """account_id 不存在时应立即抛出 ValueError，不调用 acquire。"""
    pool, a0, a1 = _pool_with_accounts()

    acquire_called = []

    async def _fake_acquire(exclude=None):
        acquire_called.append(True)
        return a0

    pool.acquire = _fake_acquire

    with pytest.raises(ValueError):
        asyncio.run(pool.generate("hi", "gemini-pro", account_id="no-such-account"))

    # acquire 不应被调用
    assert acquire_called == []


def test_generate_stream_pinned_account_passes_gem_id():
    """流式版本：验证 gem_id 透传 + exclude 预填锁定逻辑。"""
    pool, a0, a1 = _pool_with_accounts()

    received_excludes = []

    async def _fake_acquire(exclude=None):
        received_excludes.append(exclude)
        return a1

    async def _fake_release(account, success, cooldown=False):
        pass

    pool.acquire = _fake_acquire
    pool.release = _fake_release

    async def _collect():
        events = []
        async for evt in pool.generate_stream("hi", "gemini-pro", gem_id="g7", account_id="account-1"):
            events.append(evt)
        return events

    events = asyncio.run(_collect())

    # 1. gem_id 透传到目标账号 client
    assert a1.client.calls == [("generate_stream", "hi", "gemini-pro", "g7")]
    assert a0.client.calls == []

    # 2. 收到了流式事件
    assert any(e.get("type") == "delta" for e in events)

    # 3. 锁定逻辑：exclude 包含除 account-1 外的所有账号
    assert len(received_excludes) == 1
    assert received_excludes[0] == {"account-0"}


def test_update_gem_routes_to_account():
    """update_gem 应路由到指定账号的 client。"""
    pool, a0, a1 = _pool_with_accounts()
    result = asyncio.run(pool.update_gem("account-1", "gem-x", "MyGem", "do stuff", "desc"))
    assert result is True
    assert a1.client.calls == [("update_gem", "gem-x", "MyGem", "do stuff", "desc")]
    assert a0.client.calls == []


def test_delete_gem_routes_to_account():
    """delete_gem 应路由到指定账号的 client。"""
    pool, a0, a1 = _pool_with_accounts()
    result = asyncio.run(pool.delete_gem("account-0", "gem-y"))
    assert result is True
    assert a0.client.calls == [("delete_gem", "gem-y")]
    assert a1.client.calls == []


def test_browser_profile_refresh_hot_updates_and_persists():
    class CookieClient:
        is_healthy = False
        cookie_credentials = ("old-psid", "old-psidts")

        async def reload_cookies(self, psid, psidts):
            self.is_healthy = True
            self.cookie_credentials = (psid, psidts)
            return {"success": True}

    pool = AccountPool()
    account = Account("account-0", "old-psid", "old-psidts", client=CookieClient(), status=AccountStatus.EXPIRED)
    pool._accounts = [account]
    pool._request_browser_profile = AsyncMock(return_value={
        "psid": "new-psid",
        "psidts": "new-psidts",
        "updated_at": "2026-08-05T00:00:00+00:00",
    })
    saved = []
    pool._save_to_file = lambda: saved.append(True)

    result = asyncio.run(pool.refresh_account_browser("account-0"))

    assert result["success"] is True
    assert (account.psid, account.psidts) == ("new-psid", "new-psidts")
    assert account.status == AccountStatus.ACTIVE
    assert account.browser_profile_status == "ready"
    assert saved == [True]


def test_browser_profile_failure_notifies_maintenance():
    class CookieClient:
        is_healthy = True
        cookie_credentials = ("old-psid", "old-psidts")

    pool = AccountPool()
    account = Account("account-0", "old-psid", "old-psidts", client=CookieClient(), status=AccountStatus.ACTIVE)
    pool._accounts = [account]
    pool._request_browser_profile = AsyncMock(side_effect=RuntimeError("browser unavailable"))
    notices = []

    async def notify(failed_account, error):
        notices.append((failed_account.id, error))
        return {"sent": True, "error": ""}

    pool.set_browser_failure_notifier(notify)
    result = asyncio.run(pool.refresh_account_browser("account-0"))

    assert result["success"] is False
    assert result["notification"]["sent"] is True
    assert notices == [("account-0", "browser unavailable")]
    assert account.status == AccountStatus.ACTIVE


def test_manual_browser_capture_updates_exact_credentials_and_status():
    class CookieClient:
        is_healthy = True
        cookie_credentials = ("old-psid", "old-psidts")

        async def reload_cookies(self, psid, psidts):
            self.cookie_credentials = (psid, psidts)
            return {"success": True}

    pool = AccountPool()
    account = Account("account-0", "old-psid", "old-psidts", client=CookieClient())
    pool._accounts = [account]
    pool._request_browser_service = AsyncMock(side_effect=[
        {"viewer_path": "/vnc.html?autoconnect=1"},
        {"psid": "manual-psid", "psidts": "manual-psidts", "updated_at": "2026-08-05T01:00:00+00:00"},
    ])
    pool._save_to_file = lambda: None

    async def run_flow():
        opened = await pool.open_account_browser("account-0")
        captured = await pool.capture_account_browser("account-0")
        return opened, captured, pool.get_status(include_credentials=True)["accounts"][0]

    opened, captured, credentials = asyncio.run(run_flow())

    assert opened["success"] is True
    assert captured["success"] is True
    assert account.browser_profile_status == "ready"
    assert credentials["psid"] == "manual-psid"
    assert credentials["psidts"] == "manual-psidts"


def test_browser_profile_ready_state_survives_restart(tmp_path, monkeypatch):
    class HealthyClient:
        is_healthy = True

        def __init__(self, **_kwargs):
            pass

        async def initialize(self):
            pass

    profile = tmp_path / "data" / "browser_profiles" / "account-0"
    profile.mkdir(parents=True)
    (profile / "gemini2api-profile.json").write_text(
        '{"profile_id":"account-0","updated_at":"2026-08-05T00:00:00+00:00"}', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    account = Account("account-0", "psid", "psidts")
    pool = AccountPool()

    with patch("app.core.account_pool.GeminiWebClient", HealthyClient):
        asyncio.run(pool._init_account_client(account))

    assert account.browser_profile_status == "ready"
    assert account.browser_profile_updated_at == "2026-08-05T00:00:00+00:00"


def test_flow_style_browser_page_has_interactive_controls():
    html = (
        Path(__file__).resolve().parents[2] / "refresher" / "session_browser.html"
    ).read_text(encoding="utf-8")

    for marker in (
        'id="complete"',
        'id="capsLock"',
        'id="paste"',
        'id="copyRemote"',
        'id="reconnect"',
        'id="fullscreen"',
        "new RFB(screen, websocketUrl",
        "rfb.clipboardPasteFrom(text)",
        "rfb.addEventListener('clipboard', handleRemoteClipboard)",
        "window.opener.postMessage",
    ):
        assert marker in html


def test_refresher_startup_cleans_stale_x_display():
    script = (
        Path(__file__).resolve().parents[2] / "refresher" / "start.sh"
    ).read_text(encoding="utf-8")

    assert "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99" in script


def test_refresher_clears_only_chromium_singleton_markers():
    source = (
        Path(__file__).resolve().parents[2] / "refresher" / "refresher.py"
    ).read_text(encoding="utf-8")

    assert '("SingletonLock", "SingletonCookie", "SingletonSocket")' in source
    assert "_clear_stale_chromium_locks(profile_dir)" in source
