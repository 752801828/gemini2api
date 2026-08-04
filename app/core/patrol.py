"""Scheduled text and image-text patrols for every configured account."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import random
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.utils.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

TEXT_PROMPTS = [
    "这是一次服务盘巡测试。请只回复：盘巡正常",
    "请计算 17 × 23，只回复计算结果。",
    "请用一句不超过 20 个字的话说明什么是人工智能。",
    "请列出春、夏、秋、冬四个季节，用中文顿号分隔。",
    "请返回一个合法 JSON：包含 status 字段，值为 ok。不要使用 Markdown。",
    "请把“服务运行正常”翻译成英文，只回复译文。",
]
IMAGE_PROMPTS = [
    "请描述这些图片中的主要物体、颜色和场景。",
    "请逐张概括图片内容，并指出图片之间最明显的共同点或差异。",
    "请说明这些图片里最醒目的视觉元素，不要臆测看不见的信息。",
    "请为每张图片写一句简短、客观的中文说明。",
    "请判断这些图片大致属于什么场景，并给出简要依据。",
]
MODELS = ["gemini-pro", "gemini-flash", "gemini-flash-thinking", "gemini-flash-lite"]
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_CONFIG = {
    "enabled": False,
    "interval_minutes": 60,
    "text_test_enabled": True,
    "image_test_enabled": True,
    "models": ["gemini-flash"],
    "image_min_count": 1,
    "image_max_count": 5,
    "notify_enabled": True,
    "webhook_url": "",
    "webhook_secret": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class PatrolService:
    def __init__(self, account_pool, data_dir: str | Path = "data"):
        self.account_pool = account_pool
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "patrol_config.json"
        self.history_path = self.data_dir / "patrol_history.json"
        self.images_path = self.data_dir / "patrol_images.json"
        self.images_dir = self.data_dir / "patrol_images"
        self.config = self._load_json(self.config_path, DEFAULT_CONFIG)
        if self.config.get("model"):
            self.config["models"] = [self.config["model"]]
        self.config.pop("model", None)
        self.history = self._load_json(self.history_path, [])
        if not isinstance(self.history, list):
            self.history = []
        self.images = self._load_json(self.images_path, [])
        if not isinstance(self.images, list):
            self.images = []
        self.current: dict | None = None
        self.next_run_at: str | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._round_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    @staticmethod
    def _load_json(path: Path, default):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(default, dict) and isinstance(loaded, dict):
                return {**default, **loaded}
            return loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return copy.deepcopy(default)

    def _save_config(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.config_path, json.dumps(self.config, ensure_ascii=False, indent=2))

    def _save_history(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # ponytail: JSON is sufficient here; move to SQLite if 1,000 retained rounds become a real limit.
        self.history = self.history[-1000:]
        atomic_write_text(self.history_path, json.dumps(self.history, ensure_ascii=False, indent=2))

    def _save_images(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.images_path, json.dumps(self.images, ensure_ascii=False, indent=2))

    def list_images(self) -> list[dict]:
        available = []
        for item in self.images:
            if (self.images_dir / item.get("stored_name", "")).is_file():
                available.append({key: item[key] for key in ("id", "name", "mime", "size", "uploaded_at")})
        return available

    def add_image(self, name: str, data: bytes) -> dict:
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片大小必须在 1 字节到 10 MB 之间")
        mime = _detect_image_mime(data)
        if not mime:
            raise ValueError("仅支持 PNG、JPEG、GIF 和 WebP 图片")
        image_id = secrets.token_hex(8)
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[mime]
        stored_name = image_id + ext
        self.images_dir.mkdir(parents=True, exist_ok=True)
        (self.images_dir / stored_name).write_bytes(data)
        item = {
            "id": image_id,
            "name": Path(name).name[:120] or stored_name,
            "stored_name": stored_name,
            "mime": mime,
            "size": len(data),
            "uploaded_at": _now(),
        }
        self.images.append(item)
        self._save_images()
        return {key: item[key] for key in ("id", "name", "mime", "size", "uploaded_at")}

    def get_image(self, image_id: str) -> tuple[Path, str] | None:
        item = next((item for item in self.images if item.get("id") == image_id), None)
        if not item:
            return None
        path = self.images_dir / item.get("stored_name", "")
        return (path, item["mime"]) if path.is_file() else None

    def delete_image(self, image_id: str) -> bool:
        item = next((item for item in self.images if item.get("id") == image_id), None)
        if not item:
            return False
        (self.images_dir / item.get("stored_name", "")).unlink(missing_ok=True)
        self.images.remove(item)
        self._save_images()
        return True

    async def start(self) -> None:
        if not self._scheduler_task or self._scheduler_task.done():
            self._stopping = False
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in (self._scheduler_task, self._round_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def public_config(self) -> dict:
        config = {k: v for k, v in self.config.items() if k not in {"webhook_url", "webhook_secret"}}
        config["webhook_configured"] = bool(self.config.get("webhook_url"))
        config["secret_configured"] = bool(self.config.get("webhook_secret"))
        return config

    def update_config(self, updates: dict) -> dict:
        allowed = set(DEFAULT_CONFIG) | {"clear_webhook"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")

        merged = {**self.config}
        clear = bool(updates.pop("clear_webhook", False))
        for key, value in updates.items():
            if key in {"webhook_url", "webhook_secret"} and not str(value or "").strip():
                continue
            merged[key] = value.strip() if isinstance(value, str) else value
        if clear:
            merged["webhook_url"] = ""
            merged["webhook_secret"] = ""
        if not (merged.get("text_test_enabled") or merged.get("image_test_enabled")):
            raise ValueError("至少启用一种测试")
        interval = int(merged.get("interval_minutes", 0))
        if not 1 <= interval <= 10080:
            raise ValueError("盘巡间隔需在 1 到 10080 分钟之间")
        models = list(dict.fromkeys(merged.get("models") or []))
        if not models or any(model not in MODELS for model in models):
            raise ValueError("至少选择一个有效测试模型")
        merged["models"] = models
        image_min = int(merged.get("image_min_count", 1))
        image_max = int(merged.get("image_max_count", 5))
        if not 1 <= image_min <= image_max <= 5:
            raise ValueError("图片随机张数需满足 1 ≤ 最少张数 ≤ 最多张数 ≤ 5")
        if merged.get("webhook_url"):
            parsed = urlparse(merged["webhook_url"])
            if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/"):
                raise ValueError("飞书 Webhook 地址无效")
        self.config = merged
        self._save_config()
        self._wake.set()
        return self.public_config()

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            if not self.config.get("enabled"):
                self.next_run_at = None
                self._wake.clear()
                await self._wake.wait()
                continue
            delay = max(60, int(self.config["interval_minutes"]) * 60)
            self.next_run_at = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                continue
            except asyncio.TimeoutError:
                pass
            round_id = self.launch_round("scheduled")
            if round_id and self._round_task:
                try:
                    await self._round_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Scheduled patrol round failed")

    def launch_round(self, trigger: str = "manual") -> str | None:
        if self._round_task and not self._round_task.done():
            return None
        round_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self._round_task = asyncio.create_task(self._run_round(round_id, trigger))
        return round_id

    async def _run_round(self, round_id: str, trigger: str) -> None:
        started = time.perf_counter()
        accounts = self.account_pool.get_status().get("accounts", [])
        test_types = []
        if self.config.get("text_test_enabled"):
            test_types.append("text")
        if self.config.get("image_test_enabled"):
            test_types.append("image")
        self.current = {
            "id": round_id,
            "trigger": trigger,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "duration_ms": None,
            "total": len(accounts) * len(test_types),
            "success": 0,
            "failed": 0,
            "notification": {"sent": False, "error": ""},
            "tasks": [],
        }
        try:
            await asyncio.gather(*(self._test_account(account, test_types) for account in accounts))
            self.current["status"] = "success" if self.current["failed"] == 0 else "partial"
            if self.current["total"] == 0:
                self.current["status"] = "empty"
        except asyncio.CancelledError:
            self.current["status"] = "cancelled"
            raise
        except Exception as exc:
            self.current["status"] = "failed"
            self.current["error"] = _safe_error(exc)
            logger.exception("Patrol round failed")
        finally:
            self.current["finished_at"] = _now()
            self.current["duration_ms"] = round((time.perf_counter() - started) * 1000)
            if self.config.get("notify_enabled") and self.config.get("webhook_url"):
                self.current["notification"] = await self._notify_feishu(self.current)
            self.history.append(copy.deepcopy(self.current))
            self._save_history()

    async def _test_account(self, account: dict, test_types: list[str]) -> None:
        for test_type in test_types:
            started = time.perf_counter()
            result = {
                "account_id": account.get("id", ""),
                "account_label": account.get("label") or account.get("id", ""),
                "type": test_type,
                "success": False,
                "duration_ms": 0,
                "response_preview": "",
                "error": "",
                "model": "",
                "prompt": "",
                "image_samples": [],
            }
            try:
                attachments = None
                model = random.choice(self.config.get("models") or ["gemini-flash"])
                prompt = random.choice(TEXT_PROMPTS)
                if test_type == "image":
                    images = self.list_images()
                    if not images:
                        raise ValueError("图片素材库为空，请先上传测试图片")
                    count = random.randint(self.config.get("image_min_count", 1), self.config.get("image_max_count", 5))
                    selected = random.sample(images, min(count, len(images)))
                    prompt = random.choice(IMAGE_PROMPTS)
                    attachments = []
                    for image in selected:
                        source = self.get_image(image["id"])
                        if source:
                            path, mime = source
                            attachments.append({"data": path.read_bytes(), "filename": image["name"], "mime": mime})
                    if not attachments:
                        raise ValueError("选中的图片素材不可用")
                    result["image_samples"] = [image["name"] for image in selected]
                result["model"] = model
                result["prompt"] = prompt
                response = await self.account_pool.generate(
                    prompt,
                    model,
                    attachments=attachments,
                    account_id=account.get("id"),
                )
                text = str(response.get("text", "")).strip()
                result["success"] = bool(text)
                result["response_preview"] = text[:300]
                if not text:
                    result["error"] = "模型返回空内容"
            except Exception as exc:
                result["error"] = _safe_error(exc)
            result["duration_ms"] = round((time.perf_counter() - started) * 1000)
            self.current["tasks"].append(result)
            self.current["success" if result["success"] else "failed"] += 1

    async def _notify_feishu(self, round_data: dict) -> dict:
        webhook = self.config.get("webhook_url", "")
        secret = self.config.get("webhook_secret", "")
        timestamp = str(int(time.time()))
        payload = {"msg_type": "text", "content": {"text": self._notification_text(round_data)}}
        if secret:
            digest = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
            payload.update({"timestamp": timestamp, "sign": base64.b64encode(digest).decode()})
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(webhook, json=payload)
            response.raise_for_status()
            body = response.json()
            code = body.get("code", body.get("StatusCode", 0)) if isinstance(body, dict) else 0
            if code not in (0, "0", None):
                raise RuntimeError(body.get("msg") or body.get("StatusMessage") or f"Feishu code {code}")
            return {"sent": True, "error": ""}
        except Exception as exc:
            logger.warning("Feishu patrol notification failed: %s", _safe_error(exc))
            return {"sent": False, "error": _safe_error(exc)}

    @staticmethod
    def _notification_text(round_data: dict) -> str:
        types = {"text": [0, 0], "image": [0, 0]}
        failed_accounts = []
        for task in round_data.get("tasks", []):
            bucket = types[task["type"]]
            bucket[1] += 1
            if task["success"]:
                bucket[0] += 1
            else:
                failed_accounts.append(f"{task['account_label']}({task['type']})")
        lines = [
            "【Gemini2API 盘巡完成】",
            f"轮次：{round_data['id']}",
            f"触发：{'定时' if round_data['trigger'] == 'scheduled' else '手动'}",
            f"耗时：{round_data.get('duration_ms', 0) / 1000:.2f} 秒",
            f"任务：{round_data['success']}/{round_data['total']} 成功",
            f"文字：{types['text'][0]}/{types['text'][1]}，图文：{types['image'][0]}/{types['image'][1]}",
        ]
        if failed_accounts:
            lines.append("失败：" + "、".join(failed_accounts[:10]))
        return "\n".join(lines)

    def overview(self, history_limit: int = 50) -> dict:
        today = datetime.now().astimezone().date()
        today_rounds = []
        for item in self.history:
            try:
                if datetime.fromisoformat(item["started_at"]).astimezone().date() == today:
                    today_rounds.append(item)
            except (KeyError, TypeError, ValueError):
                continue
        latest = self.current or (self.history[-1] if self.history else None)
        return {
            "config": self.public_config(),
            "images": self.list_images(),
            "running": bool(self._round_task and not self._round_task.done()),
            "next_run_at": self.next_run_at,
            "current": copy.deepcopy(latest),
            "stats": {
                "today": {
                    "rounds": len(today_rounds),
                    "tasks": sum(x.get("total", 0) for x in today_rounds),
                    "success": sum(x.get("success", 0) for x in today_rounds),
                },
                "current": {
                    "tasks": latest.get("total", 0) if latest else 0,
                    "success": latest.get("success", 0) if latest else 0,
                    "failed": latest.get("failed", 0) if latest else 0,
                },
                "history": {
                    "rounds": len(self.history),
                    "tasks": sum(x.get("total", 0) for x in self.history),
                    "success": sum(x.get("success", 0) for x in self.history),
                },
            },
            "history": copy.deepcopy(list(reversed(self.history[-history_limit:]))),
        }
