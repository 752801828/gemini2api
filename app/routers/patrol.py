from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/admin/patrol", tags=["Patrol"])


class PatrolConfigUpdate(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    text_test_enabled: bool | None = None
    image_test_enabled: bool | None = None
    model: Literal["gemini-pro", "gemini-flash", "gemini-flash-thinking", "gemini-flash-lite"] | None = None
    notify_enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=500)
    webhook_secret: str | None = Field(default=None, max_length=200)
    clear_webhook: bool = False


def _service(request: Request):
    service = getattr(request.app.state, "patrol", None)
    if service is None:
        raise HTTPException(status_code=503, detail="盘巡服务尚未启动")
    return service


@router.get("")
async def get_patrol(request: Request, history_limit: int = Query(default=50, ge=1, le=200)):
    return _service(request).overview(history_limit)


@router.put("/config")
async def update_patrol_config(request: Request, update: PatrolConfigUpdate):
    try:
        values = {key: value for key, value in update.model_dump(exclude_unset=True).items() if value is not None}
        config = _service(request).update_config(values)
        return {"status": "ok", "config": config}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run")
async def run_patrol(request: Request):
    round_id = _service(request).launch_round("manual")
    if not round_id:
        raise HTTPException(status_code=409, detail="已有一轮盘巡正在执行")
    return {"status": "started", "round_id": round_id}
