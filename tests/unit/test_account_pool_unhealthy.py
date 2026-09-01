"""issue #11 回归守卫：账号 status=ACTIVE 但 client 会话已失效时的池行为。

线上现象：跑一阵后所有请求 529 `All accounts busy (max_concurrent=8), waited 60.0s`，
面板显示 cookie 没过期、换新 cookie 才恢复，且只有 1 个账号（并发槽位根本不可能打满）。

真因不是槽位泄漏，而是 `AccountPool` 的两个谓词不一致：
  - `_find_available()` 要求 `status == ACTIVE` **且** `client.is_healthy`
  - "是不是只是忙"的判断只看 `status == ACTIVE`
于是 `status=ACTIVE` 但 `client._healthy=False` 的账号：对 `_find_available` 不可见、却被算作
"有活跃账号" → 每个请求都进排队分支 → 空等满 acquire_timeout → 报一条槽位占用为 0 的假 529。
三条恢复路径（救 EXPIRED / client 自愈 / PSIDTS 轮换）在这个状态下全部够不着，故永久卡死。

本文件按修复项分组守卫：
  F1+F3 → test 1（精确消息、快速失败）、test 2（路由级状态码/文案 + 无槽位泄漏）
  F1 零回归 → test 4（真·健康且满载的池行为必须与修复前逐字一致）
  F2 → test 3（自愈可达性：reload_cookies 恰好被调用一次且请求随后成功）
  F4 → test 5（刷新失败不得把本来健康的 client 永久拉黑）
  F6 → test 6（客户端断连不得算作账号失败）
"""
import asyncio
import time

import pytest

from app.core.account_pool import Account, AccountPool, AccountStatus
from app.core.gemini_client import NO_HEALTHY_ACCOUNT_MSG, classify_error

_AUTH = {"Authorization": "Bearer sk-test-key"}


class _FakeClient:
    """最小 GeminiWebClient 替身：只实现池会用到的健康/重载/生成三件事。"""

    def __init__(self, healthy: bool = True, reload_succeeds: bool = False):
        self._healthy = healthy
        self._reload_succeeds = reload_succeeds
        self.reload_calls = 0
        self.generate_calls = 0
        # 每次进入 reload_cookies 时回调（用于探测调用现场，比如池锁是否被持有）
        self.on_reload = None
        # 模拟 reload_cookies 的网络耗时（真实实现是 60s 级超时的网络 I/O）
        self.reload_delay = 0.0

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    async def reload_cookies(self, psid=None, psidts=None) -> dict:
        self.reload_calls += 1
        if self.on_reload is not None:
            self.on_reload()
        if self.reload_delay:
            await asyncio.sleep(self.reload_delay)
        if self._reload_succeeds:
            self._healthy = True
            return {"success": True}
        return {"success": False, "error": "Cookie expired - redirected to Google login page"}

    async def generate(self, prompt, model, conversation_id="", attachments=None, gem_id=None,
                       extended_thinking=False) -> dict:
        self.generate_calls += 1
        return {"text": "ok", "images": [], "conversation_id": "c1"}

    async def generate_stream(self, prompt, model, conversation_id="", attachments=None, gem_id=None,
                              extended_thinking=False):
        self.generate_calls += 1
        yield {"type": "delta", "text": "hello"}
        yield {"type": "final", "text": "hello", "images": [], "conversation_id": "c1"}


def _make_pool(clients, *, max_concurrent: int = 8, acquire_timeout: float = 5.0) -> AccountPool:
    pool = AccountPool()
    pool._accounts = [
        Account(id=f"account-{i}", psid=f"psid-{i}", psidts="", label=f"account-{i}",
                status=AccountStatus.ACTIVE, client=c)
        for i, c in enumerate(clients)
    ]
    pool._max_concurrent = max_concurrent
    pool._acquire_timeout = acquire_timeout
    return pool


# ---------------------------------------------------------------------------
# 测试 1（F1+F3）：精确消息守卫
# ---------------------------------------------------------------------------

def test_unhealthy_active_account_fails_fast_with_accurate_message():
    """单账号 ACTIVE + 不健康：acquire 必须立刻失败（不是等满 5s 超时），
    文案不能是 "All accounts busy"（槽位占用是 0，那是假消息），映射也不能是 529。"""
    client = _FakeClient(healthy=False)

    async def _run():
        pool = _make_pool([client], acquire_timeout=5.0)
        t0 = time.monotonic()
        with pytest.raises(RuntimeError) as ei:
            await pool.acquire()
        return time.monotonic() - t0, str(ei.value), pool.accounts[0]

    elapsed, msg, account = asyncio.run(_run())

    # 快速失败：修复前这里会老老实实排队等满 acquire_timeout(5s) 才抛错
    assert elapsed < 1.0, f"acquire 等了 {elapsed:.2f}s，应当立即失败"
    assert "All accounts busy" not in msg
    assert msg == NO_HEALTHY_ACCOUNT_MSG
    # 槽位从头到尾就没被占用过 —— 证明"忙"是假的
    assert account.active_requests == 0

    status, err_type, retry_after = classify_error(RuntimeError(msg))
    assert status != 529 and err_type != "overloaded_error"
    assert (status, err_type, retry_after) == (503, "api_error", None)


# ---------------------------------------------------------------------------
# 测试 2（F1+F3）：路由级
# ---------------------------------------------------------------------------

def test_route_reports_session_expired_not_overloaded(gem_client, monkeypatch):
    """POST /v1/chat/completions 打到"ACTIVE 但会话失效"的池：
    必须回准确文案 + 503，而不是 529 overloaded；并且账号槽位仍是 0（不是泄漏）。"""
    import app.routers.openai as oai

    client = _FakeClient(healthy=False)
    pool = _make_pool([client], acquire_timeout=5.0)
    monkeypatch.setattr(oai, "gemini_client", pool)

    t0 = time.monotonic()
    r = gem_client.post("/v1/chat/completions", json={
        "model": "gemini-pro", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"请求耗时 {elapsed:.2f}s，不应空等 acquire_timeout"
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["error"]["message"] == NO_HEALTHY_ACCOUNT_MSG
    assert body["error"]["type"] == "api_error"
    assert "All accounts busy" not in r.text
    # 529 会让客户端无限重试一个不会自愈的池，故绝不能带 Retry-After
    assert "retry-after" not in {k.lower() for k in r.headers}
    # 不是并发槽位泄漏：请求结束后占用仍为 0
    assert pool.accounts[0].active_requests == 0


# ---------------------------------------------------------------------------
# 测试 3（F2）：自愈可达性
# ---------------------------------------------------------------------------

def test_unhealthy_account_self_heals_via_reload_cookies_exactly_once():
    """ACTIVE 但会话失效 + reload_cookies 能成功 → 必须恰好自愈一次并让请求成功。
    修复前这里是 0 次：acquire 从不把不健康账号交出去，client 内的自愈是死代码。"""
    client = _FakeClient(healthy=False, reload_succeeds=True)
    lock_held_during_reload = []

    async def _run():
        pool = _make_pool([client], acquire_timeout=5.0)
        # 锁安全探针：reload_cookies 是 60s 级网络 I/O，持池锁调用会卡死所有账号的所有请求
        client.on_reload = lambda: lock_held_during_reload.append(pool._cond.locked())
        result = await pool.generate("hi", "gemini-pro")
        return result, pool.accounts[0]

    result, account = asyncio.run(_run())

    assert client.reload_calls == 1
    assert client.generate_calls == 1
    assert result["text"] == "ok"
    assert account.status == AccountStatus.ACTIVE
    assert account.active_requests == 0
    assert lock_held_during_reload == [False], "reload_cookies 绝不能在持有 self._cond 时调用"


def test_concurrent_requests_trigger_at_most_one_heal():
    """单飞：N 个并发请求撞上同一个失效账号，最多只触发一次 reload_cookies（不能是 N 次
    网络风暴）；没抢到自愈的那些请求立刻拿到准确报错，而不是陪等一次网络 I/O。"""
    client = _FakeClient(healthy=False, reload_succeeds=True)
    client.reload_delay = 0.2

    async def _run():
        pool = _make_pool([client], acquire_timeout=5.0)
        t0 = time.monotonic()
        results = await asyncio.gather(
            *[pool.generate("hi", "gemini-pro") for _ in range(5)],
            return_exceptions=True,
        )
        return results, time.monotonic() - t0, pool.accounts[0]

    results, elapsed, account = asyncio.run(_run())

    assert client.reload_calls == 1, f"自愈被触发了 {client.reload_calls} 次，应当单飞"
    oks = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(oks) == 1 and len(errs) == 4
    # 没抢到自愈的请求给的是准确文案，不是假的"忙"
    assert all(str(e) == NO_HEALTHY_ACCOUNT_MSG for e in errs)
    assert elapsed < 2.0, f"并发自愈耗时 {elapsed:.2f}s，不应串行叠加"
    assert account.active_requests == 0


def test_failed_heal_does_not_retry_on_every_request():
    """自愈失败要进冷却：后续请求不能每一个都再打一次 Google（既慢又抬高风控面），
    且必须立刻报准确错误，不能自旋。"""
    client = _FakeClient(healthy=False, reload_succeeds=False)

    async def _run():
        pool = _make_pool([client], acquire_timeout=5.0)
        msgs = []
        t0 = time.monotonic()
        for _ in range(3):
            with pytest.raises(RuntimeError) as ei:
                await pool.acquire()
            msgs.append(str(ei.value))
        return msgs, time.monotonic() - t0, pool.accounts[0]

    msgs, elapsed, account = asyncio.run(_run())

    assert client.reload_calls == 1, "自愈失败后应进入冷却，不能每个请求都重试一次"
    assert msgs == [NO_HEALTHY_ACCOUNT_MSG] * 3
    assert elapsed < 1.0
    assert account.healing is False
    assert account.heal_cooldown_until > 0


# ---------------------------------------------------------------------------
# 测试 4（F1 零回归）：真·健康且满载的池，行为必须与修复前逐字一致
# ---------------------------------------------------------------------------

def test_saturated_healthy_pool_still_queues_and_is_woken_by_release():
    """全部 ACTIVE 且健康、槽位打满 → 必须排队等待，并在 release() 时被唤醒拿到槽位。"""
    async def _run():
        pool = _make_pool([_FakeClient(healthy=True)], max_concurrent=1, acquire_timeout=5.0)
        first = await pool.acquire()
        assert first.active_requests == 1

        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.1)
        assert not waiter.done(), "健康但满载的池必须排队，不能立即失败"

        await pool.release(first, success=True)
        second = await asyncio.wait_for(waiter, timeout=2.0)
        return second

    account = asyncio.run(_run())
    assert account.active_requests == 1
    assert account.status == AccountStatus.ACTIVE


def test_saturated_healthy_pool_timeout_keeps_original_busy_error_and_529():
    """健康但满载且无人 release → 超时后仍是原来的 All accounts busy 文案，仍映射 529。"""
    async def _run():
        pool = _make_pool([_FakeClient(healthy=True)], max_concurrent=2, acquire_timeout=0.3)
        await pool.acquire()
        await pool.acquire()
        t0 = time.monotonic()
        with pytest.raises(RuntimeError) as ei:
            await pool.acquire()
        return time.monotonic() - t0, str(ei.value), pool.accounts[0]

    elapsed, msg, account = asyncio.run(_run())

    # 真忙就该真等：等满 acquire_timeout 才报错（不能被 F1 改成秒失败）
    assert elapsed >= 0.3, f"只等了 {elapsed:.2f}s，健康满载的池必须排队等满超时"
    assert msg == "All accounts busy (max_concurrent=2), waited 0.3s"
    assert account.active_requests == 2
    assert classify_error(RuntimeError(msg)) == (529, "overloaded_error", 30)


def _prepare_client_for_reload(monkeypatch, *, token_after_reload: str):
    """造一个"当前健康、会话可用"的真 GeminiWebClient，并把 reload_cookies 里的网络动作打桩。

    token_after_reload 为空串表示这次重载拿不到 SNlM0e token（重载失败）。
    """
    import app.core.gemini_client as gc

    class _Jar:
        def __init__(self):
            self.sets = []

        def set(self, name, value, **kwargs):
            self.sets.append((name, value))

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            return None

    monkeypatch.setattr(gc, "AsyncSession", _Session)
    client = gc.GeminiWebClient(psid="good-psid", psidts="good-psidts")
    client._cookie_jar = _Jar()
    client._http = _Session()
    client._session_token = "GOOD-TOKEN"
    client._healthy = True

    async def _obtain():
        client._session_token = token_after_reload
        if not token_after_reload:
            client._last_reload_error = "Cookie expired - redirected to Google login page"

    async def _rotate():
        return False

    async def _heartbeat():
        return None

    monkeypatch.setattr(client, "_obtain_session_token", _obtain)
    monkeypatch.setattr(client, "_rotate_cookies", _rotate)
    monkeypatch.setattr(client, "_send_heartbeat", _heartbeat)
    monkeypatch.setattr(client, "_ensure_refresh_task", lambda: None)
    return client


# ---------------------------------------------------------------------------
# 测试 5（F4）：刷新失败不得把本来健康的 client 永久拉黑
# ---------------------------------------------------------------------------

def test_failed_reload_does_not_down_a_healthy_client(monkeypatch):
    """reload_cookies 在任何网络动作前就 _healthy = False，失败返回路径却不恢复：
    一次失败的刷新（面板误填 cookie / Google 临时抽风）就永久少掉一个健康账号。
    修复后失败必须"原样不动"：健康标志、会话 token、cookie 全部回滚。"""
    client = _prepare_client_for_reload(monkeypatch, token_after_reload="")

    result = asyncio.run(client.reload_cookies(psid="bad-psid", psidts="bad-psidts"))

    assert result["success"] is False
    assert client.is_healthy is True, "刷新失败不得把本来健康的 client 拉黑"
    # 只回滚 _healthy 是不够的：token/cookie 也得回滚，否则是个"健康但发不出请求"的假账号
    assert client._session_token == "GOOD-TOKEN"
    assert client._psid == "good-psid"
    assert client._psidts == "good-psidts"
    assert ("__Secure-1PSID", "good-psid") in client._cookie_jar.sets


def test_failed_reload_keeps_an_already_unhealthy_client_unhealthy(monkeypatch):
    """回滚只针对"本来就健康"的 client：本来不健康的（F2 自愈路径）失败后仍是不健康，
    不能被回滚成假健康。"""
    client = _prepare_client_for_reload(monkeypatch, token_after_reload="")
    client._healthy = False
    client._session_token = ""

    result = asyncio.run(client.reload_cookies())

    assert result["success"] is False
    assert client.is_healthy is False


def test_successful_reload_still_applies_new_cookies(monkeypatch):
    """零回归：成功的重载必须照常换上新 cookie 与新会话，不能被回滚逻辑误伤。"""
    client = _prepare_client_for_reload(monkeypatch, token_after_reload="NEW-TOKEN")

    result = asyncio.run(client.reload_cookies(psid="new-psid", psidts="new-psidts"))

    assert result["success"] is True
    assert client.is_healthy is True
    assert client._session_token == "NEW-TOKEN"
    assert client._psid == "new-psid"
    assert client._psidts == "new-psidts"


# ---------------------------------------------------------------------------
# 测试 6（F6）：客户端断连不得算作账号失败
# ---------------------------------------------------------------------------

class _DisconnectingClient(_FakeClient):
    """模拟"请求被取消 / 客户端中途断连"的 client。"""

    async def generate(self, prompt, model, conversation_id="", attachments=None, gem_id=None,
                       extended_thinking=False):
        self.generate_calls += 1
        raise asyncio.CancelledError()

    async def generate_stream(self, prompt, model, conversation_id="", attachments=None, gem_id=None,
                              extended_thinking=False):
        self.generate_calls += 1
        yield {"type": "delta", "text": "partial"}
        await asyncio.sleep(30)  # 客户端在这里断开，生成器被 aclose
        yield {"type": "final", "text": "partial", "images": [], "conversation_id": "c1"}


def test_client_disconnect_is_not_an_account_failure():
    """断连 3 次（取消 + 流中途 aclose）：账号必须仍是 ACTIVE、连续失败没涨、槽位归 0。
    修复前 finally 里统一 release(success=False)，3 次就把单账号池标成 EXPIRED
    —— 用户点 3 次"停止"就能把服务搞挂。"""
    client = _DisconnectingClient(healthy=True)

    async def _run():
        pool = _make_pool([client])
        account = pool.accounts[0]

        # ① 非流式：请求被取消
        for _ in range(3):
            try:
                await pool.generate("hi", "gemini-pro")
            except asyncio.CancelledError:
                pass
            assert account.active_requests == 0

        # ② 流式：吐了一块之后客户端断连（async generator 被 aclose → GeneratorExit）
        for _ in range(3):
            agen = pool.generate_stream("hi", "gemini-pro")
            first = await agen.__anext__()
            assert first["type"] == "delta"
            await agen.aclose()
            assert account.active_requests == 0

        return account

    account = asyncio.run(_run())

    assert account.status == AccountStatus.ACTIVE, "断连不该把账号标成 EXPIRED"
    assert account.consecutive_failures == 0
    assert account.error_count == 0
    assert account.active_requests == 0


def test_real_failures_still_count_against_the_account():
    """零回归反向守卫：真正的失败仍要累加，满 3 次仍标 EXPIRED —— F6 只豁免断连。"""
    class _BrokenClient(_FakeClient):
        async def generate(self, prompt, model, conversation_id="", attachments=None, gem_id=None,
                           extended_thinking=False):
            raise ValueError("upstream exploded")

    async def _run():
        pool = _make_pool([_BrokenClient(healthy=True)])
        account = pool.accounts[0]
        for _ in range(3):
            with pytest.raises(ValueError):
                await pool.generate("hi", "gemini-pro")
        return account

    account = asyncio.run(_run())

    assert account.consecutive_failures == 3
    assert account.error_count == 3
    assert account.status == AccountStatus.EXPIRED
    assert account.active_requests == 0
