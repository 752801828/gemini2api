from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import httpx

from app.config import settings
from app.core.account_pool import AccountStatus


class FlowBridgeError(RuntimeError):
    def __init__(self, error_type: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


class FlowBridgeService:
    def __init__(
        self,
        account_pool,
        *,
        enabled: bool | None = None,
        base_url: str | None = None,
        secret: str | None = None,
        timeout: float | None = None,
    ):
        self.account_pool = account_pool
        self.enabled = settings.flow_bridge_enabled if enabled is None else enabled
        self.base_url = (settings.flow_bridge_base_url if base_url is None else base_url).strip().rstrip("/")
        self.secret = (settings.flow_bridge_secret if secret is None else secret).strip()
        self.timeout = max(5.0, settings.flow_bridge_timeout if timeout is None else timeout)
        self._locks: dict[int, asyncio.Lock] = {}
        self._failure_notifier = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.secret)

    def verify_secret(self, authorization: Optional[str]) -> bool:
        provided = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
        return bool(self.configured and secrets.compare_digest(provided, self.secret))

    async def aclose(self) -> None:
        await self._http.aclose()

    def set_failure_notifier(self, notifier) -> None:
        self._failure_notifier = notifier

    async def _notify_failure(self, token_id: int, error: Exception) -> None:
        if not self._failure_notifier:
            return
        account = self.account_pool.get_flow_account(token_id) or SimpleNamespace(
            id=f"flow-{token_id}",
            label=f"Flow #{token_id}",
        )
        try:
            await self._failure_notifier(account, str(error)[:300])
        except Exception:
            pass

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured:
            raise FlowBridgeError("bridge_not_configured", "Flow bridge is not enabled or configured", 503)
        try:
            response = await self._http.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.secret}"},
                json=payload,
            )
        except httpx.HTTPError as error:
            raise FlowBridgeError("flow_unavailable", f"Flow bridge is unavailable: {error.__class__.__name__}", 503) from error
        if response.is_success:
            try:
                body = response.json()
            except ValueError as error:
                raise FlowBridgeError("invalid_flow_response", "Flow bridge returned invalid JSON", 502) from error
            return body if isinstance(body, dict) else {}
        try:
            detail = response.json().get("detail", {})
        except ValueError:
            detail = {}
        message = detail.get("message") if isinstance(detail, dict) else ""
        error_type = detail.get("error_type") if isinstance(detail, dict) else ""
        raise FlowBridgeError(
            str(error_type or "flow_bridge_error"),
            str(message or f"Flow bridge request failed (HTTP {response.status_code})"),
            response.status_code,
        )

    async def list_accounts(self) -> list[dict]:
        body = await self._request("GET", "/api/gemini-bridge/accounts")
        return [item for item in body.get("accounts", []) if isinstance(item, dict)]

    async def refresh_token(self, flow_token_id: int) -> dict:
        token_id = int(flow_token_id)
        if token_id <= 0:
            raise FlowBridgeError("invalid_mapping", "Invalid Flow token id", 422)
        lock = self._locks.setdefault(token_id, asyncio.Lock())
        async with lock:
            try:
                request_id = uuid4().hex
                result = await self._request(
                    "POST",
                    f"/api/gemini-bridge/accounts/{token_id}/refresh",
                    {"request_id": request_id},
                )
                if result.get("request_id") and result.get("request_id") != request_id:
                    raise FlowBridgeError("request_mismatch", "Flow returned mismatched refresh metadata", 409)
                account = self.account_pool.get_flow_account(token_id)
                if account is None:
                    raise FlowBridgeError("callback_missing", "Flow completed without sending Gemini cookies", 502)
                return {"success": True, "account_id": account.id, "flow_token_id": token_id}
            except Exception as error:
                if isinstance(error, FlowBridgeError) and (
                    error.error_type in {"account_disabled", "token_disabled", "disabled"}
                    or "account is disabled" in str(error).lower()
                ):
                    account = self.account_pool.get_flow_account(token_id)
                    if account is not None:
                        account.status = AccountStatus.DISABLED
                        account.last_error = str(error)[:300]
                    raise
                await self._notify_failure(token_id, error)
                raise

    async def refresh_account(self, account) -> dict:
        if account.source != "flow" or not account.flow_token_id:
            raise FlowBridgeError("not_flow_account", "Only Flow-backed accounts can use this refresh", 409)
        return await self.refresh_token(account.flow_token_id)

    async def sync_accounts(self) -> dict:
        rows = await self.list_accounts()
        results = []
        for row in rows:
            if not row.get("enabled", True) or not row.get("auth_ready") or not row.get("profile_id"):
                continue
            try:
                results.append(await self.refresh_token(int(row.get("flow_token_id"))))
            except Exception as error:
                results.append({
                    "success": False,
                    "flow_token_id": row.get("flow_token_id"),
                    "error": str(error)[:300],
                })
        failed = sum(1 for item in results if not item.get("success"))
        return {
            "success": failed == 0,
            "available": len(rows),
            "refreshed": len(results) - failed,
            "failed": failed,
            "results": results,
        }

    async def accept_cookie_callback(self, payload: dict) -> dict:
        try:
            token_id = int(payload.get("flow_token_id"))
        except (TypeError, ValueError) as error:
            raise FlowBridgeError("invalid_callback", "Invalid Flow token id", 422) from error
        if token_id <= 0:
            raise FlowBridgeError("invalid_callback", "Invalid Flow token id", 422)
        psid = str(payload.get("__Secure-1PSID") or "").strip()
        psidts = str(payload.get("__Secure-1PSIDTS") or "").strip()
        if not psid:
            raise FlowBridgeError("cookie_missing", "__Secure-1PSID is required", 422)
        try:
            account = await self.account_pool.upsert_flow_account(
                token_id,
                psid=psid,
                psidts=psidts,
                email=str(payload.get("email") or ""),
                name=str(payload.get("name") or ""),
            )
        except Exception as error:
            raise FlowBridgeError("cookie_rejected", f"Gemini rejected Flow cookies: {error}", 409) from error
        return {"success": True, "account_id": account.id, "flow_token_id": token_id}
