from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, PositiveInt

from app.core.flow_bridge import FlowBridgeError, FlowBridgeService


internal_router = APIRouter(prefix="/internal/flow-bridge", tags=["Flow bridge"])
admin_router = APIRouter(prefix="/admin/flow-bridge", tags=["Flow bridge admin"])
_service: FlowBridgeService | None = None


class FlowSyncRequest(BaseModel):
    flow_token_ids: list[PositiveInt] | None = Field(default=None, max_length=200)


def set_service(service: FlowBridgeService) -> None:
    global _service
    _service = service


def _get_service() -> FlowBridgeService:
    if _service is None:
        raise FlowBridgeError("bridge_unavailable", "Flow bridge is not initialized", 503)
    return _service


def _error(error: FlowBridgeError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"type": error.error_type, "message": str(error)}},
    )


@internal_router.get("/health")
async def health(authorization: Optional[str] = Header(None)):
    service = _get_service()
    if not service.verify_secret(authorization):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid Flow bridge secret"}})
    return {"success": True, "configured": service.configured}


@internal_router.post("/accounts/cookies")
async def receive_cookies(payload: dict, authorization: Optional[str] = Header(None)):
    service = _get_service()
    if not service.verify_secret(authorization):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid Flow bridge secret"}})
    try:
        return await service.accept_cookie_callback(payload)
    except FlowBridgeError as error:
        return _error(error)


@admin_router.get("/status")
async def status():
    service = _get_service()
    return {
        "enabled": service.enabled,
        "configured": service.configured,
        "base_url": service.base_url,
    }


@admin_router.get("/accounts")
async def list_accounts():
    try:
        return {"accounts": await _get_service().list_accounts()}
    except FlowBridgeError as error:
        return _error(error)


@admin_router.post("/sync")
async def sync_accounts(payload: FlowSyncRequest | None = None):
    try:
        return await _get_service().sync_accounts(payload.flow_token_ids if payload else None)
    except FlowBridgeError as error:
        return _error(error)


@admin_router.post("/accounts/{account_id}/refresh")
async def refresh_account(account_id: str):
    account = _get_service().account_pool._get_account(account_id)
    if account is None:
        return JSONResponse(status_code=404, content={"error": {"message": "Account not found"}})
    try:
        return await _get_service().refresh_account(account)
    except FlowBridgeError as error:
        return _error(error)
