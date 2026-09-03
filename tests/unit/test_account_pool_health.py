import asyncio

import pytest

from app.core.account_pool import Account, AccountPool, AccountStatus
from app.core.gemini_client import NO_HEALTHY_ACCOUNT_MSG


class UnhealthyClient:
    is_healthy = False
    cookie_credentials = ("old-psid", "old-psidts")

    def __init__(self):
        self._heal_lock = asyncio.Lock()

    async def check_account(self):
        return {"valid": False}

    async def reload_cookies(self, _psid=None, _psidts=None):
        return {"success": False, "error": "proxy unavailable"}


def test_unhealthy_active_account_is_not_reported_as_busy():
    pool = AccountPool()
    account = Account(
        "account-0",
        "psid",
        "psidts",
        status=AccountStatus.ACTIVE,
        client=UnhealthyClient(),
    )
    pool._accounts = [account]

    async def acquire_and_read_status():
        with pytest.raises(RuntimeError, match=NO_HEALTHY_ACCOUNT_MSG.replace("(", r"\(").replace(")", r"\)")):
            await pool.acquire()
        return pool.get_status()

    status = asyncio.run(acquire_and_read_status())
    assert account.status == AccountStatus.ACTIVE
    assert pool.active_count == 0
    assert status["accounts"][0]["status"] == "expired"


def test_failed_cookie_reload_expires_account_immediately():
    pool = AccountPool()
    account = Account(
        "account-0",
        "psid",
        "psidts",
        status=AccountStatus.ACTIVE,
        client=UnhealthyClient(),
    )

    with pytest.raises(RuntimeError, match="proxy unavailable"):
        asyncio.run(pool._apply_account_cookies(account, "new-psid", "new-psidts"))

    assert account.status == AccountStatus.EXPIRED
    assert account.last_error == "proxy unavailable"
