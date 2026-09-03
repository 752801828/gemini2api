import json
import time
import asyncio
import logging
import httpx
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.gemini_client import GeminiWebClient, HTTPStatusError, NO_HEALTHY_ACCOUNT_MSG
from app.config import settings
from app.core.usage_metrics import live_metrics
from app.utils.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

MAX_REQUEST_ACCOUNT_ATTEMPTS = 3
# 自愈（reload_cookies）失败后的冷却秒数（issue #11 F2）。账号真的死了（cookie 被 Google
# 吊销、要人工换号）时，避免每个请求都去打一次 RotateCookies —— 既拖慢每个请求，
# 又平白抬高对 Google 的敲门频率（风控面）。
HEAL_RETRY_COOLDOWN = 60.0

# healing 单飞标志的陈旧阈值（issue #11 H1 belt-and-braces）。reload_cookies 是 60s 级
# 网络 I/O，正常情况下这个标志活不过那么久；万一某条路径（未来的新 bug）没能在结束时
# 清掉它，超过这个阈值就当陈旧、可回收 —— 双保险，绝不指望它成为常态触发路径。
HEALING_STALE_SECONDS = 120.0


def _is_5xx(exc: Exception) -> bool:
    """判断异常是否为 5xx（含 Google 503 限流），这类可换账号 failover 重试。"""
    return isinstance(exc, HTTPStatusError) and 500 <= exc.status_code < 600


def _is_retryable(exc: Exception) -> bool:
    """Errors safe to retry on another account before a response is emitted."""
    if _is_5xx(exc):
        return True
    if isinstance(exc, HTTPStatusError) and exc.status_code == 429:
        return True
    if isinstance(exc, RuntimeError):
        return True
    if isinstance(exc, HTTPStatusError) and exc.status_code in (401, 403):
        return True
    return False


class AccountStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"
    REFRESHING = "refreshing"


class RotationStrategy(str, Enum):
    ROUND_ROBIN = "round-robin"
    FAILOVER = "failover"


@dataclass
class Account:
    id: str
    psid: str
    psidts: str
    label: str = ""
    status: AccountStatus = AccountStatus.ACTIVE
    request_count: int = 0
    error_count: int = 0
    consecutive_failures: int = 0
    active_requests: int = 0
    last_used: datetime | None = None
    last_error: str = ""
    cookie_updated_at: str | None = None
    # 被 5xx/503 限流后的冷却截止时间戳（loop.time()）；冷却期内不优先选，但不算 expired
    cooldown_until: float = 0.0
    # 会话自愈（reload_cookies）的单飞标志：为真表示已有请求在锁外跑自愈，其余并发请求
    # 不再重复触发（issue #11 F2）
    healing: bool = False
    # healing 置为 True 时的 loop.time() 时间戳，供 _pick_heal_candidate() 做陈旧回收
    # 判断（issue #11 H1 belt-and-braces）
    healing_started_at: float = 0.0
    # 自愈失败后的冷却截止时间戳（loop.time()）；冷却期内不再触发自愈
    heal_cooldown_until: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    client: GeminiWebClient | None = field(default=None, repr=False)
    browser_profile_status: str = "standby"
    browser_profile_updated_at: str | None = None
    browser_profile_error: str = ""
    source: str = "manual"
    flow_token_id: int | None = None
    flow_email: str = ""
    flow_proxy_node_id: int | None = None
    flow_proxy_node_name: str = ""
    flow_proxy_url: str = ""
    flow_route_fingerprint: str = ""


class AccountPool:
    def __init__(self):
        self._accounts: list[Account] = []
        # Condition 自带一把锁，既保护账号列表的并发访问，又用于并发满载时排队等待。
        self._cond = asyncio.Condition()
        self._robin_index = 0
        # 单调递增的 id 计数器：用 len() 生成 id 在删除中间账号后会与现存 id 撞号，
        # 故改用永不回退的计数器，确保 id 全程唯一（见 add_account）。
        self._next_id_seq = 0
        self._strategy = RotationStrategy(settings.rotation_strategy)
        self._max_concurrent = settings.max_concurrent_per_account
        # 并发满载时排队等待上限（秒）。等不到可用槽位才报错，而不是立即拒绝，
        # 让 agent 的高并发请求排队通过而非撞 "No available accounts" 失败。
        self._acquire_timeout = settings.acquire_timeout
        # 持有后台 fire-and-forget task 的强引用，防止被 GC 中途回收
        self._bg_tasks: set = set()
        self._browser_refresh_locks: dict[str, asyncio.Lock] = {}
        self._browser_failure_notifier = None
        self._flow_cookie_refresher = None
        self._request_count = 0

    @property
    def accounts(self) -> list[Account]:
        return list(self._accounts)

    @property
    def active_count(self) -> int:
        return sum(
            1 for a in self._accounts
            if a.status == AccountStatus.ACTIVE
            and a.client is not None
            and a.client.is_healthy
        )

    @property
    def total_count(self) -> int:
        return len(self._accounts)

    async def initialize(self):
        accounts_path = Path(settings.accounts_file)
        if accounts_path.exists():
            self._load_from_file(accounts_path)
            logger.info(f"Loaded {len(self._accounts)} accounts from {accounts_path}")
        else:
            self._add_from_env()

        for account in self._accounts:
            await self._init_account_client(account)

        active = self.active_count
        logger.info(f"Account pool ready: {active}/{self.total_count} active")

    def _load_from_file(self, path: Path):
        # 整文件损坏（半截 JSON/断电）时容错：记录日志后当作空池，绝不让单个坏文件
        # 在 initialize() 期间抛异常把整个进程启动卡死（VULN-010 读容错）。
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.error(f"accounts file {path} is corrupt, starting with empty pool: {e}")
            return
        accounts_data = data if isinstance(data, list) else data.get("accounts", [])
        for i, item in enumerate(accounts_data):
            # 单条坏记录（非 dict / 缺 psid）跳过并告警，保留其余有效账号。
            try:
                if not isinstance(item, dict):
                    raise TypeError("not an object")
                psid = item.get("psid")
                if not psid:
                    raise KeyError("psid")
                account = Account(
                    id=item.get("id", f"account-{i}"),
                    psid=psid.strip().strip('"').strip("'").rstrip(";"),
                    psidts=(item.get("psidts") or "").strip().strip('"').strip("'").rstrip(";"),
                    label=item.get("label", f"account-{i}"),
                    source=item.get("source", "manual"),
                    flow_token_id=item.get("flow_token_id"),
                    flow_email=item.get("flow_email", ""),
                    flow_proxy_node_id=item.get("flow_proxy_node_id"),
                    flow_proxy_node_name=item.get("flow_proxy_node_name", ""),
                    flow_proxy_url=item.get("flow_proxy_url", ""),
                    flow_route_fingerprint=item.get("flow_route_fingerprint", ""),
                    cookie_updated_at=item.get("cookie_updated_at"),
                )
            except Exception as e:
                logger.warning(f"Skipping corrupt account entry #{i} in {path}: {e}")
                continue
            self._accounts.append(account)
        # 把计数器推到所有现存 account-N 后端的下一位，避免后续 add_account 撞号。
        self._sync_id_seq()

    def _sync_id_seq(self):
        """让 _next_id_seq 大于所有现存 'account-<n>' 的数字后缀，保证后续生成的 id 唯一。"""
        max_seq = -1
        for a in self._accounts:
            if a.id.startswith("account-"):
                suffix = a.id[len("account-"):]
                if suffix.isdigit():
                    max_seq = max(max_seq, int(suffix))
        self._next_id_seq = max(self._next_id_seq, max_seq + 1)

    def _add_from_env(self):
        account = Account(
            id="account-0",
            psid=settings.gemini_psid,
            psidts=settings.gemini_psidts,
            label="Default (env)",
            cookie_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._accounts.append(account)

    async def _init_account_client(self, account: Account):
        profile_root = Path("data/browser_profiles").resolve()
        profile_meta = (profile_root / account.id / "gemini2api-profile.json").resolve()
        if profile_root in profile_meta.parents:
            try:
                metadata = json.loads(profile_meta.read_text(encoding="utf-8"))
                if metadata.get("profile_id") == account.id and metadata.get("updated_at"):
                    account.browser_profile_status = "ready"
                    account.browser_profile_updated_at = metadata["updated_at"]
            except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
                pass

        async def refresh_profile() -> bool:
            return bool((await self._refresh_account_credentials(account)).get("success"))

        if account.source == "flow" and not account.flow_proxy_url:
            account.client = None
            account.status = AccountStatus.EXPIRED
            account.last_error = "Flow fixed proxy route has not been synchronized"
            logger.warning("Account %s is waiting for its Flow fixed proxy route", account.id)
            if account in self._accounts and self._flow_cookie_refresher:
                task = asyncio.create_task(self._refresh_failed_account(account))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            return

        client = GeminiWebClient(
            psid=account.psid,
            psidts=account.psidts,
            browser_refresh=refresh_profile,
            proxy_url=account.flow_proxy_url,
        )
        account.client = client
        await client.initialize()
        if client.is_healthy:
            account.status = AccountStatus.ACTIVE
            logger.info(f"Account {account.id} ({account.label}) initialized")
        else:
            account.status = AccountStatus.EXPIRED
            logger.warning(f"Account {account.id} ({account.label}) failed to initialize")
            if account in self._accounts and (account.source == "flow" or settings.browser_refresh_enabled):
                task = asyncio.create_task(
                    self._refresh_failed_account(account)
                    if account.source == "flow"
                    else self._refresh_account_browser(account)
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

    def _find_available(self, exclude: set | None = None, preferred_id: str | None = None) -> Account | None:
        """在已持有 self._cond 锁的前提下，挑一个未满载的 ACTIVE 账号；没有则返回 None。
        exclude: 本次 failover 中已试过失败的账号 id，跳过。
        preferred_id: 优先选择的账号；不可用时直接选择其他空闲账号。
        冷却中的账号（被 5xx 限流）降级为兜底：优先选非冷却的，全冷却了才选冷却的。
        """
        exclude = exclude or set()
        now = asyncio.get_event_loop().time()
        candidates = [
            a for a in self._accounts
            if a.status == AccountStatus.ACTIVE
            and a.client is not None and a.client.is_healthy
            and a.active_requests < self._max_concurrent
            and a.id not in exclude
        ]
        if not candidates:
            return None
        fresh = [a for a in candidates if a.cooldown_until <= now]
        pool = fresh if fresh else candidates  # 优先非冷却；全冷却则用冷却的兜底
        if preferred_id:
            preferred = next((a for a in pool if a.id == preferred_id), None)
            if preferred is not None:
                return preferred
        if self._strategy == RotationStrategy.ROUND_ROBIN:
            return self._pick_round_robin(pool)
        return self._pick_failover(pool)

    def _unhealthy_active(self) -> list[Account]:
        """status 仍是 ACTIVE、但 client 会话已失效的账号（需已持有 self._cond 锁）。

        这些账号对 _find_available() 不可见（它要求 client.is_healthy），却又不是 EXPIRED，
        是 issue #11 里池"永久卡死"的那批账号。
        """
        return [
            a for a in self._accounts
            if a.status == AccountStatus.ACTIVE
            and a.client is not None and not a.client.is_healthy
        ]

    def _pick_heal_candidate(self) -> Account | None:
        """挑一个可尝试自愈的账号，并就地打上 healing 单飞标志（需已持有 self._cond 锁）。

        单飞：同一账号同一时刻最多只有一个请求在跑 reload_cookies，其余并发请求直接拿到
        准确报错，而不是排队陪等一次 60s 级网络 I/O（issue #11 F2）。
        失败过的账号在 HEAL_RETRY_COOLDOWN 内不再被选中，避免永久失效的账号把每个请求
        都变成一次对 Google 的 RotateCookies。

        H1 belt-and-braces：healing 标志本身已经被设计成不依赖锁就能清掉（见
        _try_heal_unhealthy 的 finally），但这里仍加一道陈旧回收保险——万一某条未来路径
        还是没能清掉它，超过 HEALING_STALE_SECONDS 就当作陈旧、重新可选，绝不让单个 bug
        把自愈通道永久焊死。
        """
        now = asyncio.get_event_loop().time()
        for a in self._unhealthy_active():
            if a.healing:
                if now - a.healing_started_at <= HEALING_STALE_SECONDS:
                    continue
                logger.warning(
                    f"Account {a.id} healing flag stale for "
                    f"{now - a.healing_started_at:.0f}s, reclaiming it (H1 staleness guard)"
                )
            elif a.heal_cooldown_until > now:
                continue
            a.healing = True
            a.healing_started_at = now
            return a
        return None

    async def _try_heal_unhealthy(self, account: Account) -> bool:
        """对单个「ACTIVE 但会话失效」的账号跑一次 reload_cookies（issue #11 F2）。

        **必须在 self._cond 之外调用**：reload_cookies 会重建 HTTP 会话并访问 Google，
        超时是 60s 级别；持锁执行会把整个池——包括所有健康账号的请求——一起卡死。
        调用方须先用 _pick_heal_candidate() 打好 healing 标志；本函数负责清除它。

        用 reload_cookies 而不是 check_account：后者只拿现有 cookie 重读，不会轮换，
        救不了已经被 Google 轮换掉的 PSIDTS（这正是 issue #11 里"换新 cookie 才恢复"的成因）。

        M3：过 client 自己的 _heal_lock（而不是只靠池级 Account.healing）——两把锁保护的
        并发面不一样：Account.healing 只挡同一账号的池级并发请求，挡不住 client 内部
        generate()/generate_stream() 开头那段自愈逻辑走另一条调用链同时跑 reload_cookies，
        两次并发 reload_cookies 会互相踩会话状态（_http 被关两次、cookie/token 写串）。
        拿到 _heal_lock 后二次确认健康状态：可能在等锁期间已经被另一条路径治好了。
        """
        client = account.client
        ok = False
        cancelled = False
        try:
            if client is not None:
                async with client._heal_lock:
                    if client.is_healthy:
                        ok = True
                    else:
                        result = await client.reload_cookies()
                        ok = bool(result and result.get("success")) and client.is_healthy
        except asyncio.CancelledError:
            # H1（reviewer FIX_FIRST）：取消不是"自愈失败"——不知道 reload_cookies 本来
            # 会不会成功，不该给账号判 HEAL_RETRY_COOLDOWN。跳过下面需要锁的记账部分，
            # 只清标志、原样把取消传播出去。
            cancelled = True
            raise
        except Exception as e:
            logger.warning(f"Account {account.id} self-heal failed: {e}")
        finally:
            # H1：healing 标志是不需要锁的普通属性写，必须无条件、同步地放在这里——不能
            # 包进下面 `async with self._cond:` 里面。Starlette 客户端断连触发的取消会在
            # finally 内的每个 await 点反复注入 CancelledError；若清标志这一步本身要先抢一把
            # 被占用的锁，取消就能让它永远跑不到，自愈通道被永久焊死——等于把 issue #11
            # 换个姿势报出来（这正是本次修复要堵的洞）。
            account.healing = False
            if not cancelled:
                heal_cooldown_until = (
                    0.0 if ok else asyncio.get_event_loop().time() + HEAL_RETRY_COOLDOWN
                )
                # 计数器归零/冷却时间/notify_all 都需要锁，做成 best-effort：shield 保证
                # "这次调用又被取消一次"也不会让它半途而废，但也绝不会因为抢不到锁就拖住
                # 取消的传播——拿不到就在后台慢慢拿，前台该抛的取消照抛不误。
                await asyncio.shield(self._finish_heal(account, ok, heal_cooldown_until))
        return ok

    async def _finish_heal(self, account: Account, ok: bool, heal_cooldown_until: float) -> None:
        """_try_heal_unhealthy 结果落盘中需要锁的部分（计数器 + notify_all）。

        故意与 healing 标志的清除（不需要锁）分开：调用方用 asyncio.shield 包裹本函数，
        取消不会让它半途而废，也不会阻塞取消本身的传播。
        """
        async with self._cond:
            if ok:
                account.consecutive_failures = 0
                logger.info(f"Account {account.id} self-healed: cookies reloaded")
                # 多出了可用槽位，唤醒所有排队者重新评估
                self._cond.notify_all()
            else:
                account.heal_cooldown_until = heal_cooldown_until

    async def _try_recover_expired(self):
        """无可用账号时，尝试恢复 EXPIRED 账号（已持有锁）。"""
        for a in self._accounts:
            if (
                a.client
                and a.status in {AccountStatus.ACTIVE, AccountStatus.EXPIRED}
                and (a.status == AccountStatus.EXPIRED or not a.client.is_healthy)
            ):
                try:
                    result = await a.client.check_account()
                    if result.get("valid"):
                        a.status = AccountStatus.ACTIVE
                        a.consecutive_failures = 0
                        logger.info(f"Account {a.id} recovered during acquire")
                    else:
                        a.status = AccountStatus.EXPIRED
                except Exception:
                    a.status = AccountStatus.EXPIRED

    async def acquire(self, exclude: set | None = None, preferred_id: str | None = None) -> Account:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._acquire_timeout
        # 每次 acquire 最多触发一次自愈：失败了就按 F3 诚实报错，绝不在这里反复重试自旋。
        heal_attempted = False
        while True:
            heal_target: Account | None = None
            async with self._cond:
                while True:
                    account = self._find_available(exclude, preferred_id)
                    if account is not None:
                        account.active_requests += 1
                        account.last_used = datetime.now(timezone.utc)
                        return account

                    # failover 场景：主动排除了部分账号后没有候选了 → 不排队不救活，
                    # 立即报错让 failover 循环停止（已无其他账号可试）
                    if exclude:
                        raise RuntimeError("No more accounts to failover to")

                    # 没有空闲槽位。区分两种情况：
                    #   ① 有「可用账号」但都满载 → 排队等 release 唤醒（不要跑网络恢复，
                    #      否则高并发满载时每次唤醒都串行跑 check_account 把整个池卡死）
                    #   ② 一个可用账号都没有 → 才尝试救活（网络 I/O，低频路径）
                    # F1（issue #11）：这里的谓词必须与 _find_available() 完全一致——也要求
                    # client.is_healthy。否则 status=ACTIVE 但会话已失效的账号会被当成"只是忙"，
                    # 每个请求都白等满 acquire_timeout(60s) 再报 "All accounts busy"，而实际
                    # 占用槽位是 0；那条 529 是假的，且因为恢复路径永远够不着而变成永久卡死。
                    has_available_account = any(
                        a.status == AccountStatus.ACTIVE
                        and a.client is not None and a.client.is_healthy
                        for a in self._accounts
                    )
                    if not has_available_account:
                        unhealthy = self._unhealthy_active()
                        if not unhealthy:
                            # 真正的"一个 ACTIVE 账号都没有"（其余是 EXPIRED/DISABLED）：
                            # 这是修复前就存在的持锁网络恢复路径，不是本次改动引入的新东西，
                            # 维持原样——check_account 比 reload_cookies 轻量得多，且只有在
                            # 彻底没有 ACTIVE 账号时才会走到这里。
                            await self._try_recover_expired()
                            account = self._find_available(preferred_id=preferred_id)
                            if account is not None:
                                account.active_requests += 1
                                account.last_used = datetime.now(timezone.utc)
                                return account
                            raise RuntimeError("No available accounts")
                        # H2（reviewer FIX_FIRST）：这里绝不能再调 _try_recover_expired()。
                        # F1 把 has_available_account 的判定收紧成"ACTIVE 且健康"后，池里
                        # 只要同时存在 EXPIRED 账号和 ACTIVE-但-不健康 账号，就会撞进这个分支；
                        # _try_recover_expired 对每个 EXPIRED 账号跑 check_account() 是持锁
                        # 网络 I/O，没有冷却/单飞保护，会把整个池卡住，还对 Google 形成
                        # 逐请求重试的敲门风暴。ACTIVE-但-不健康 的账号已经有 F2 那条锁外、
                        # 单飞、带冷却的自愈路径，直接走它，不碰 EXPIRED 账号的恢复。
                        if not heal_attempted:
                            heal_target = self._pick_heal_candidate()
                            if heal_target is not None:
                                break
                        # 自愈已试过/正被别的请求跑/还在冷却 → F3：给出准确文案，
                        # 让 classify_error 映射成 503，而不是会诱发无限重试的 529。
                        raise RuntimeError(NO_HEALTHY_ACCOUNT_MSG)

                    # 有可用账号但都满载 → 排队等可用槽位，而非直接拒绝
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise RuntimeError(
                            f"All accounts busy (max_concurrent={self._max_concurrent}), "
                            f"waited {self._acquire_timeout}s"
                        )
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            f"All accounts busy (max_concurrent={self._max_concurrent}), "
                            f"waited {self._acquire_timeout}s"
                        )

            # ↑ 到这里锁已释放。reload_cookies 是 60s 级网络 I/O，绝不能持锁跑。
            heal_attempted = True
            # M1（reviewer MEDIUM）：不能让自愈把 acquire() 的总耗时顶穿 operator 配置的
            # acquire_timeout（最多可能多等一整个 reload_cookies 时长，~60s）。用剩余预算
            # 给它设一个硬上限；预算已经耗尽就干脆不发起，直接报准确错误。
            remaining = deadline - loop.time()
            if remaining <= 0:
                heal_target.healing = False
                raise RuntimeError(NO_HEALTHY_ACCOUNT_MSG)
            try:
                # shield：即使这次 wait_for 超时/被取消，_try_heal_unhealthy 也会在后台
                # 跑完——healing 标志的清除、冷却时间的落盘都不会因为我们不再等它而丢失。
                await asyncio.wait_for(
                    asyncio.shield(self._try_heal_unhealthy(heal_target)), timeout=remaining
                )
            except asyncio.TimeoutError:
                pass  # 没在预算内跑完：这次 acquire() 按预算诚实超时，不再空等
            # 回到外层循环重新取锁、重新 _find_available()：
            # 自愈成功 → 直接拿到槽位；失败/超预算 → 下一轮走上面的 NO_HEALTHY_ACCOUNT_MSG 分支。

    async def release(self, account: Account, success: bool, cooldown: bool = False):
        async with self._cond:
            account.active_requests = max(0, account.active_requests - 1)
            account.request_count += 1
            if success:
                account.consecutive_failures = 0
            elif cooldown:
                # 5xx/503 限流：不是账号坏，只是被 Google 临时限流。
                # 设短期冷却（期间降级不优先选），不累积失败、不标 expired。
                account.error_count += 1
                account.cooldown_until = asyncio.get_event_loop().time() + settings.failover_cooldown
                logger.warning(
                    f"Account {account.id} cooled down for {settings.failover_cooldown}s (5xx rate-limit)"
                )
            else:
                account.error_count += 1
                account.consecutive_failures += 1
                if account.consecutive_failures >= 3:
                    account.status = AccountStatus.EXPIRED
                    logger.warning(f"Account {account.id} marked expired after 3 consecutive failures")
            # 释放了一个槽位，只唤醒一个排队的等待者即可（notify(1) 避免惊群：
            # notify_all 会让所有等待者一起醒来争抢同一个空位，落败者再重新 wait，
            # 在高并发满载时造成无谓的反复唤醒/竞争）
            self._cond.notify(1)

    async def release_disconnected(self, account: Account):
        """中性释放：客户端断连/请求被取消时只归还槽位，不计成功也不计失败（issue #11 F6）。

        断连是客户端的行为，不是账号的错。走 release(success=False) 会累加
        consecutive_failures，满 3 次就把账号标成 EXPIRED —— 用户连点 3 次"停止"
        就能把单账号池打死。也不能走 success=True：那会把之前真实的连续失败清零。
        """
        async with self._cond:
            account.active_requests = max(0, account.active_requests - 1)
            account.request_count += 1
            self._cond.notify(1)

    def _pick_round_robin(self, available: list[Account]) -> Account:
        # 在「稳定的全量账号顺序」self._accounts 上做轮转，而不是在每次调用都变长度的
        # 过滤子集 available 上取模——后者会因 busy/cooldown/exclude 的成员变动让同一个
        # _robin_index 映射到不同位置，破坏轮转公平性（同号被反复选中或别的号被跳过）。
        # 这里从上次位置之后开始扫描稳定列表，返回第一个属于 available 的账号，
        # 并把 _robin_index 钉到它在稳定列表中的位置，使轮转跨成员变化保持稳定。
        n = len(self._accounts)
        if n == 0:
            return available[0]
        available_set = set(id(a) for a in available)
        start = (self._robin_index + 1) % n
        for offset in range(n):
            idx = (start + offset) % n
            cand = self._accounts[idx]
            if id(cand) in available_set:
                self._robin_index = idx
                return cand
        # 理论不可达（available 至少含一个 self._accounts 中的账号）；兜底返回首个候选。
        return available[0]

    def _pick_failover(self, available: list[Account]) -> Account:
        for a in self._accounts:
            if a in available:
                return a
        return available[0]

    async def add_account(self, psid: str, psidts: str, label: str = "") -> Account:
        # 用单调计数器生成 id，删除中间账号后也绝不撞号（不再用易撞号的 len()）。
        self._sync_id_seq()
        account_id = f"account-{self._next_id_seq}"
        self._next_id_seq += 1
        account = Account(
            id=account_id,
            psid=psid.strip().strip('"').strip("'").rstrip(";"),
            psidts=psidts.strip().strip('"').strip("'").rstrip(";"),
            label=label or account_id,
            cookie_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._init_account_client(account)
        self._accounts.append(account)
        self._save_to_file()
        return account

    def get_flow_account(self, flow_token_id: int) -> Account | None:
        return next((a for a in self._accounts if a.source == "flow" and a.flow_token_id == int(flow_token_id)), None)

    async def upsert_flow_account(
        self,
        flow_token_id: int,
        *,
        psid: str,
        psidts: str = "",
        email: str = "",
        name: str = "",
        proxy_node_id: int | None = None,
        proxy_node_name: str = "",
        proxy_url: str = "",
        route_fingerprint: str = "",
    ) -> Account:
        token_id = int(flow_token_id)
        account = self.get_flow_account(token_id)
        label = (name or email or f"Flow #{token_id}").strip()
        fixed_proxy_url = str(proxy_url or "").strip()
        if account is not None:
            account.label = label
            account.flow_email = email.strip().lower()
            account.flow_proxy_node_id = int(proxy_node_id) if proxy_node_id is not None else None
            account.flow_proxy_node_name = str(proxy_node_name or "").strip()
            account.flow_proxy_url = fixed_proxy_url
            account.flow_route_fingerprint = str(route_fingerprint or "").strip()
            if account.client is None:
                account.psid = psid.strip()
                account.psidts = psidts.strip()
                await self._init_account_client(account)
                if not account.client or not account.client.is_healthy:
                    raise RuntimeError("Flow cookies are not a valid Gemini login session")
                account.cookie_updated_at = datetime.now(timezone.utc).isoformat()
                self._save_to_file()
            else:
                await account.client.set_proxy_url(fixed_proxy_url)
                await self._apply_account_cookies(account, psid, psidts)
            return account
        account = Account(
            id=f"flow-{token_id}",
            psid=psid.strip(),
            psidts=psidts.strip(),
            label=label,
            source="flow",
            flow_token_id=token_id,
            flow_email=email.strip().lower(),
            flow_proxy_node_id=int(proxy_node_id) if proxy_node_id is not None else None,
            flow_proxy_node_name=str(proxy_node_name or "").strip(),
            flow_proxy_url=fixed_proxy_url,
            flow_route_fingerprint=str(route_fingerprint or "").strip(),
            cookie_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._init_account_client(account)
        if not account.client or not account.client.is_healthy:
            if account.client:
                await account.client.shutdown()
            raise RuntimeError("Flow cookies are not a valid Gemini login session")
        self._accounts.append(account)
        self._save_to_file()
        return account

    async def remove_account(self, account_id: str) -> bool:
        for i, account in enumerate(self._accounts):
            if account.id == account_id:
                if account.client:
                    await account.client.shutdown()
                self._accounts.pop(i)
                self._save_to_file()
                return True
        return False

    async def _request_browser_service(self, path: str, payload: dict) -> dict:
        key = settings.admin_api_key or settings.api_key
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(timeout=settings.browser_refresh_timeout) as client:
            response = await client.post(
                f"{settings.browser_refresher_url.rstrip('/')}{path}",
                json=payload,
                headers=headers,
            )
        if response.status_code != 200:
            try:
                message = response.json().get("error")
            except Exception:
                message = response.text
            raise RuntimeError(str(message or f"Browser refresher HTTP {response.status_code}")[:300])
        return response.json()

    async def _request_browser_profile(self, account: Account) -> dict:
        psid, psidts = account.client.cookie_credentials
        data = await self._request_browser_service("/refresh", {
            "account_id": account.id,
            "label": account.label,
            "psid": psid,
            "psidts": psidts,
        })
        if not data.get("psid") or not data.get("psidts"):
            raise RuntimeError("Browser profile did not return login cookies")
        return data

    async def _browser_failure(self, account: Account, error: str, was_healthy: bool) -> dict:
        account.status = AccountStatus.ACTIVE if was_healthy else AccountStatus.EXPIRED
        account.browser_profile_status = "error"
        account.browser_profile_error = error[:300]
        logger.warning("Account %s browser profile operation failed: %s", account.id, error[:300])
        notification = None
        if self._browser_failure_notifier:
            try:
                notification = await self._browser_failure_notifier(account, error[:300])
            except Exception as notify_error:
                logger.warning("Browser maintenance notification failed: %s", str(notify_error)[:300])
        return {"success": False, "profile_id": account.id, "error": error[:300], "notification": notification}

    async def _apply_account_cookies(self, account: Account, psid: str, psidts: str) -> dict:
        try:
            result = await account.client.reload_cookies(psid, psidts)
            if not result.get("success"):
                raise RuntimeError(result.get("error", "Cookies were rejected"))
        except Exception as error:
            account.status = AccountStatus.EXPIRED
            account.last_error = str(error)[:300]
            raise
        current_psid, current_psidts = account.client.cookie_credentials
        account.psid = current_psid or psid
        account.psidts = current_psidts or psidts
        account.status = AccountStatus.ACTIVE
        account.consecutive_failures = 0
        account.last_error = ""
        account.cookie_updated_at = datetime.now(timezone.utc).isoformat()
        self._save_to_file()
        return result

    async def _refresh_account_browser(self, account: Account) -> dict:
        was_healthy = bool(account.client and account.client.is_healthy)
        if not settings.browser_refresh_enabled:
            return await self._browser_failure(account, "Built-in browser refresh is disabled", was_healthy)
        if not account.client:
            return await self._browser_failure(account, "Account client not initialized", was_healthy)

        lock = self._browser_refresh_locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            account.browser_profile_status = "refreshing"
            account.browser_profile_error = ""
            account.status = AccountStatus.REFRESHING
            try:
                data = await self._request_browser_profile(account)
                await self._apply_account_cookies(account, data["psid"], data["psidts"])
                account.browser_profile_status = "ready"
                account.browser_profile_updated_at = data.get("updated_at") or datetime.now(timezone.utc).isoformat()
                logger.info("Account %s refreshed from built-in browser profile", account.id)
                return {
                    "success": True,
                    "profile_id": account.id,
                    "updated_at": account.browser_profile_updated_at,
                }
            except Exception as e:
                return await self._browser_failure(account, str(e), was_healthy)

    async def refresh_account_browser(self, account_id: str) -> dict:
        account = self._get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        return await self._refresh_account_browser(account)

    async def open_account_browser(self, account_id: str) -> dict:
        account = self._get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        was_healthy = bool(account.client and account.client.is_healthy)
        try:
            data = await self._request_browser_service("/manual/open", {
                "account_id": account.id,
                "label": account.label,
            })
            for item in self._accounts:
                if item.browser_profile_status == "manual":
                    item.browser_profile_status = "ready" if item.browser_profile_updated_at else "standby"
            account.browser_profile_status = "manual"
            account.browser_profile_error = ""
            return {
                "success": True,
                "profile_id": account.id,
                "viewer_path": data.get("viewer_path", f"/session_browser.html?account_id={account.id}"),
            }
        except Exception as e:
            return await self._browser_failure(account, str(e), was_healthy)

    async def capture_account_browser(self, account_id: str) -> dict:
        account = self._get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        if not account.client:
            return {"success": False, "error": "Account client not initialized"}
        was_healthy = account.client.is_healthy
        try:
            data = await self._request_browser_service("/manual/capture", {"account_id": account.id})
            if not data.get("psid") or not data.get("psidts"):
                raise RuntimeError("Manual browser did not contain Gemini login cookies")
            await self._apply_account_cookies(account, data["psid"], data["psidts"])
            account.browser_profile_status = "ready"
            account.browser_profile_error = ""
            account.browser_profile_updated_at = data.get("updated_at") or datetime.now(timezone.utc).isoformat()
            return {"success": True, "profile_id": account.id, "updated_at": account.browser_profile_updated_at}
        except Exception as e:
            return await self._browser_failure(account, str(e), was_healthy)

    async def close_account_browser(self, account_id: str) -> dict:
        account = self._get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        await self._request_browser_service("/manual/close", {"account_id": account.id})
        account.browser_profile_status = "ready" if account.browser_profile_updated_at else "standby"
        return {"success": True, "profile_id": account.id}

    async def update_account_cookies(self, account_id: str, psid: str, psidts: str) -> dict:
        account = self._get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        if not account.client:
            raise RuntimeError("Account client not initialized")
        return await self._apply_account_cookies(account, psid, psidts)

    def set_browser_failure_notifier(self, notifier) -> None:
        self._browser_failure_notifier = notifier

    def set_flow_cookie_refresher(self, refresher) -> None:
        self._flow_cookie_refresher = refresher

    async def _refresh_account_credentials(self, account: Account) -> dict:
        if account.source == "flow" and self._flow_cookie_refresher:
            try:
                return await self._flow_cookie_refresher(account)
            except Exception as error:
                error_type = getattr(error, "error_type", "")
                if error_type in {"account_disabled", "token_disabled", "disabled"} or "account is disabled" in str(error).lower():
                    logger.warning("Flow account %s is disabled; falling back to its built-in browser profile", account.id)
                    account.status = AccountStatus.DISABLED
                    result = await self._refresh_account_browser(account)
                    if not result.get("success"):
                        account.status = AccountStatus.DISABLED
                    return result
                raise
        return await self._refresh_account_browser(account)

    async def _refresh_failed_account(self, account: Account) -> None:
        """Best-effort CK refresh before this request fails over to another account."""
        try:
            result = await self._refresh_account_credentials(account)
            if not result.get("success"):
                logger.warning("Account %s CK refresh failed before failover: %s", account.id, result.get("error", "unknown error"))
        except Exception as error:
            logger.warning("Account %s CK refresh failed before failover: %s", account.id, str(error)[:300])

    async def check_account(self, account_id: str) -> dict:
        for account in self._accounts:
            if account.id == account_id:
                if account.client:
                    result = await account.client.check_account()
                    if result["valid"]:
                        account.status = AccountStatus.ACTIVE
                        account.consecutive_failures = 0
                    else:
                        account.consecutive_failures += 1
                        if account.consecutive_failures >= 3:
                            account.status = AccountStatus.EXPIRED
                    return {**result, "account_id": account.id, "status": account.status.value}
                return {"valid": False, "error": "No client", "account_id": account.id}
        raise ValueError(f"Account {account_id} not found")

    async def check_all(self) -> list[dict]:
        results = []
        for account in self._accounts:
            try:
                result = await self.check_account(account.id)
                results.append(result)
            except Exception as e:
                results.append({"account_id": account.id, "valid": False, "error": str(e)})
        return results

    async def list_web_chats(self, recent: int = 300) -> list[dict]:
        """列出所有 active 账号的网页端会话（只读，用于验证/排查）。"""
        out = []
        for account in self._accounts:
            if account.status != AccountStatus.ACTIVE or not account.client:
                continue
            try:
                chats = await account.client.list_web_chats(recent=recent)
                out.append({"account_id": account.id, "count": len(chats), "chats": chats})
            except Exception as e:
                out.append({"account_id": account.id, "error": str(e)})
        return out

    async def cleanup_web_chats(self, keep_hours: float = 24.0, skip_pinned: bool = True) -> list[dict]:
        """对所有 active 账号清理超过 keep_hours 的网页会话（置顶可保留）。"""
        out = []
        for account in self._accounts:
            if account.status != AccountStatus.ACTIVE or not account.client:
                continue
            try:
                res = await account.client.cleanup_old_web_chats(
                    keep_hours=keep_hours, skip_pinned=skip_pinned
                )
                out.append({"account_id": account.id, **res})
            except Exception as e:
                out.append({"account_id": account.id, "error": str(e)})
        return out

    def _get_account(self, account_id: str):
        for a in self._accounts:
            if a.id == account_id:
                return a
        return None

    async def list_gems(self, account_id: str) -> list[dict]:
        acc = self._get_account(account_id)
        if not acc or not acc.client:
            raise ValueError(f"Account {account_id} not found or no client")
        return await acc.client.list_gems()

    async def create_gem(self, account_id: str, name: str, prompt: str, description: str = "") -> str | None:
        acc = self._get_account(account_id)
        if not acc or not acc.client:
            raise ValueError(f"Account {account_id} not found or no client")
        return await acc.client.create_gem(name, prompt, description)

    async def update_gem(self, account_id: str, gem_id: str, name: str, prompt: str, description: str = "") -> bool:
        acc = self._get_account(account_id)
        if not acc or not acc.client:
            raise ValueError(f"Account {account_id} not found or no client")
        return await acc.client.update_gem(gem_id, name, prompt, description)

    async def delete_gem(self, account_id: str, gem_id: str) -> bool:
        acc = self._get_account(account_id)
        if not acc or not acc.client:
            raise ValueError(f"Account {account_id} not found or no client")
        return await acc.client.delete_gem(gem_id)

    def set_strategy(self, strategy: str):
        self._strategy = RotationStrategy(strategy)

    def set_max_concurrent(self, value: int):
        self._max_concurrent = value
        # 提高上限后，唤醒排队等槽位的请求让它们重新检查（notify(1) 会逐个传递，
        # 这里用 notify_all 一次性放行，让所有等待者重新评估新上限）
        async def _wake():
            async with self._cond:
                self._cond.notify_all()
        try:
            task = asyncio.get_running_loop().create_task(_wake())
            # 存强引用防止 task 被 GC 中途回收，完成后自动移除
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            pass

    def get_status(self, include_credentials: bool = False) -> dict:
        accounts_info = []
        for a in self._accounts:
            client_healthy = bool(a.client and a.client.is_healthy)
            effective_status = (
                AccountStatus.EXPIRED
                if a.status == AccountStatus.ACTIVE and not client_healthy
                else a.status
            )
            info = {
                "id": a.id,
                "label": a.label,
                "psid": a.psid,
                "status": effective_status.value,
                "healthy": client_healthy,
                "request_count": a.request_count,
                "error_count": a.error_count,
                "active_requests": a.active_requests,
                "last_used": a.last_used.isoformat() if a.last_used else None,
                "cookie_updated_at": a.cookie_updated_at,
                "cooling_down": a.cooldown_until > asyncio.get_event_loop().time(),
                "models": self.models if a.client else [],
                "models_count": len(self.models) if a.client else 0,
                "browser_profile_id": a.id,
                "browser_profile_status": a.browser_profile_status,
                "browser_profile_updated_at": a.browser_profile_updated_at,
                "browser_profile_error": a.browser_profile_error,
                "source": a.source,
                "flow_token_id": a.flow_token_id,
                "flow_email": a.flow_email,
                "flow_proxy_node_id": a.flow_proxy_node_id,
                "flow_proxy_node_name": a.flow_proxy_node_name,
                "flow_proxy_bound": bool(a.flow_proxy_url),
                "flow_route_fingerprint": a.flow_route_fingerprint,
            }
            if include_credentials:
                current_psid, current_psidts = a.client.cookie_credentials if a.client else (a.psid, a.psidts)
                info["psid"] = current_psid
                info["psidts"] = current_psidts
            accounts_info.append(info)
        return {
            "total": self.total_count,
            "active": self.active_count,
            "strategy": self._strategy.value,
            "max_concurrent_per_account": self._max_concurrent,
            "request_count": self._request_count,
            "accounts": accounts_info,
        }

    async def generate(self, prompt: str, model: str, conversation_id: str = "",
                       attachments: list | None = None, gem_id: str | None = None,
                       account_id: str | None = None,
                       extended_thinking: bool = False) -> dict:
        self._request_count = getattr(self, "_request_count", 0) + 1
        request_started = time.time()
        # failover：某账号被可重试错误（5xx/未就绪/401·403）打回时，换下一个 active 账号重试，
        # 直到成功或无更多账号可试。5xx 限流账号进入冷却，401/403 标 expired。
        tried: set = set()
        # 绑定账号：排除其他所有账号，使 acquire/failover 只可能选中目标账号
        if account_id:
            if self._get_account(account_id) is None:
                raise ValueError(f"Account {account_id} not found")
        last_err = None
        account_attempts = 0
        while True:
            try:
                if account_id:
                    account = await self.acquire(exclude=tried if tried else None, preferred_id=account_id)
                else:
                    account = await self.acquire(exclude=tried if tried else None)
            except RuntimeError:
                # 没有（更多）账号可用：抛出最后一次可重试错误（若有），否则抛 acquire 的错
                if last_err is not None:
                    live_metrics.record_request(model, (time.time() - request_started) * 1000)
                    raise last_err
                raise
            account_attempts += 1
            released = False
            try:
                result = await account.client.generate(
                    prompt, model, conversation_id, attachments, gem_id,
                    extended_thinking,
                )
                live_metrics.record_request(model, (time.time() - request_started) * 1000)
                await self.release(account, success=True)
                released = True
                return result
            except (asyncio.CancelledError, GeneratorExit):
                # F6：客户端断连/取消不是账号失败，只归还槽位（否则点 3 次"停止"就能把
                # 单账号池标成 EXPIRED）。必须放在 except Exception 之前显式处理。
                await self.release_disconnected(account)
                released = True
                raise
            except Exception as e:
                if _is_retryable(e):
                    # 可重试：5xx 冷却该账号、401/403 标 expired，换下一个账号重试
                    last_err = e
                    tried.add(account.id)
                    await self.release(account, success=False, cooldown=_is_5xx(e))
                    released = True
                    if isinstance(e, HTTPStatusError) and e.status_code in (401, 403):
                        account.status = AccountStatus.EXPIRED
                    await self._refresh_failed_account(account)
                    if account_attempts >= MAX_REQUEST_ACCOUNT_ATTEMPTS:
                        live_metrics.record_request(model, (time.time() - request_started) * 1000)
                        raise last_err
                    logger.warning(f"Account {account.id} got {e}; failing over (attempt={account_attempts}/{MAX_REQUEST_ACCOUNT_ATTEMPTS})")
                    continue
                await self.release(account, success=False)
                released = True
                live_metrics.record_request(model, (time.time() - request_started) * 1000)
                raise
            finally:
                # 兜底：CancelledError/GeneratorExit 等未走上面分支的路径也归还槽位（P0-4 防泄漏死锁）
                if not released:
                    await self.release(account, success=False)

    async def generate_stream(self, prompt: str, model: str, conversation_id: str = "",
                              attachments: list | None = None, gem_id: str | None = None,
                              account_id: str | None = None, extended_thinking: bool = False):
        """真流式：持有账号槽位直到整个流结束，再 release。
        逐块产出 {"type":"delta","text":增量} ，最后产出 {"type":"final", ...}（含会话ID/图片）。

        failover：仅在「尚未向客户端 yield 任何内容前」遇到可重试错误（5xx/未就绪/401·403）才换账号重试
        （已经吐出部分内容后再换账号会导致重复，故此时只能终止）。
        """
        self._request_count = getattr(self, "_request_count", 0) + 1
        request_started = time.time()
        tried: set = set()
        # 绑定账号：排除其他所有账号，使 acquire/failover 只可能选中目标账号
        if account_id:
            if self._get_account(account_id) is None:
                raise ValueError(f"Account {account_id} not found")
        last_err = None
        account_attempts = 0
        while True:
            try:
                if account_id:
                    account = await self.acquire(exclude=tried if tried else None, preferred_id=account_id)
                else:
                    account = await self.acquire(exclude=tried if tried else None)
            except RuntimeError:
                if last_err is not None:
                    live_metrics.record_request(model, (time.time() - request_started) * 1000)
                    raise last_err
                raise
            account_attempts += 1
            emitted_any = False
            failover = False
            released = False
            try:
                for same_account_attempt in range(2):
                    try:
                        async for evt in account.client.generate_stream(
                            prompt, model, conversation_id, attachments, gem_id,
                            extended_thinking,
                        ):
                            emitted_any = True
                            yield evt
                        if not emitted_any:
                            raise RuntimeError("Gemini returned an empty stream")
                        break
                    except Exception as error:
                        if same_account_attempt == 0 and not emitted_any and _is_retryable(error):
                            logger.warning("Account %s stream failed before first chunk; quick retry once: %s", account.id, str(error)[:300])
                            await asyncio.sleep(1)
                            continue
                        raise
                live_metrics.record_request(model, (time.time() - request_started) * 1000)
                await self.release(account, success=True)
                released = True
                return
            except (asyncio.CancelledError, GeneratorExit):
                # F6：客户端在流中途断连（生成器被 aclose → GeneratorExit）或请求被取消，
                # 都不是账号的错：只归还槽位，不累加 consecutive_failures/error_count。
                await self.release_disconnected(account)
                released = True
                raise
            except Exception as e:
                # 只有「还没吐任何内容」+「可重试」+「还有别的账号」才 failover
                if _is_retryable(e) and not emitted_any:
                    last_err = e
                    tried.add(account.id)
                    await self.release(account, success=False, cooldown=_is_5xx(e))
                    released = True
                    if isinstance(e, HTTPStatusError) and e.status_code in (401, 403):
                        account.status = AccountStatus.EXPIRED
                    await self._refresh_failed_account(account)
                    if account_attempts >= MAX_REQUEST_ACCOUNT_ATTEMPTS:
                        live_metrics.record_request(model, (time.time() - request_started) * 1000)
                        raise last_err
                    logger.warning(f"Account {account.id} got {e} before first chunk; stream failing over (attempt={account_attempts}/{MAX_REQUEST_ACCOUNT_ATTEMPTS})")
                    failover = True
                else:
                    await self.release(account, success=False)
                    released = True
                    live_metrics.record_request(model, (time.time() - request_started) * 1000)
                    raise
            finally:
                # 兜底：客户端断连(GeneratorExit)/取消(CancelledError) 等路径也归还槽位（P0-4 防泄漏死锁）
                if not released:
                    await self.release(account, success=False)
            if failover:
                continue

    @property
    def models(self) -> list[str]:
        # 对外永远是固定的公开模型名（API 稳定契约），
        # 内部由 _resolve_model 按账号真实可用模型动态映射。
        from app.core.gemini_client import PUBLIC_MODELS
        return list(PUBLIC_MODELS)

    @property
    def is_healthy(self) -> bool:
        return self.active_count > 0

    def _save_to_file(self):
        accounts_data = []
        for a in self._accounts:
            accounts_data.append({
                "id": a.id,
                "psid": a.psid,
                "psidts": a.psidts,
                "label": a.label,
                "source": a.source,
                "flow_token_id": a.flow_token_id,
                "flow_email": a.flow_email,
                "flow_proxy_node_id": a.flow_proxy_node_id,
                "flow_proxy_node_name": a.flow_proxy_node_name,
                "flow_proxy_url": a.flow_proxy_url,
                "flow_route_fingerprint": a.flow_route_fingerprint,
                "cookie_updated_at": a.cookie_updated_at,
            })
        path = Path(settings.accounts_file)
        # 原子写：accounts.json 存 PSID 凭据，写入中途崩溃/断电不得截断成半截 JSON（VULN-010）。
        atomic_write_text(path, json.dumps({"accounts": accounts_data}, indent=2, ensure_ascii=False))

    async def shutdown(self):
        for account in self._accounts:
            if account.client:
                await account.client.shutdown()
        logger.info("Account pool shut down")


account_pool = AccountPool()
