import base64
import binascii
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/admin/patrol", tags=["Patrol"])


class PatrolConfigUpdate(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    text_test_enabled: bool | None = None
    image_test_enabled: bool | None = None
    text_test_count: int | None = Field(default=None, ge=1, le=20)
    image_test_count: int | None = Field(default=None, ge=1, le=20)
    models: list[Literal["gemini-pro", "gemini-pro-thinking", "gemini-flash", "gemini-flash-thinking", "gemini-flash-lite"]] | None = Field(
        default=None, min_length=1, max_length=5
    )
    image_min_count: int | None = Field(default=None, ge=1, le=5)
    image_max_count: int | None = Field(default=None, ge=1, le=5)
    notify_enabled: bool | None = None
    webhook_url: str | None = Field(default=None, max_length=500)
    webhook_secret: str | None = Field(default=None, max_length=200)
    clear_webhook: bool = False


class PatrolImageUpload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data_base64: str = Field(min_length=1, max_length=14_000_000)


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


@router.post("/images")
async def upload_patrol_image(request: Request, upload: PatrolImageUpload):
    try:
        data = base64.b64decode(upload.data_base64, validate=True)
        return {"status": "ok", "image": _service(request).add_image(upload.name, data)}
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "图片数据无效") from exc


@router.get("/images/{image_id}")
async def get_patrol_image(request: Request, image_id: str):
    image = _service(request).get_image(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")
    path, mime = image
    return FileResponse(path, media_type=mime)


@router.delete("/images/{image_id}")
async def delete_patrol_image(request: Request, image_id: str):
    if not _service(request).delete_image(image_id):
        raise HTTPException(status_code=404, detail="图片不存在")
    return {"status": "ok"}


@router.delete("/rounds/{round_id}")
async def delete_patrol_round(request: Request, round_id: str):
    if not _service(request).delete_round(round_id):
        raise HTTPException(status_code=404, detail="轮次不存在或正在执行")
    return {"status": "ok"}
