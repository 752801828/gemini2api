"""Scheduled text and image-text patrols for every configured account."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.utils.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

TEXT_PROMPT = "这是一次服务盘巡测试。请只回复：盘巡正常"
IMAGE_PROMPT = "这是一次服务盘巡测试。请简要描述图片中的主要颜色和图案。"
DEFAULT_CONFIG = {
    "enabled": False,
    "interval_minutes": 60,
    "text_test_enabled": True,
    "image_test_enabled": True,
    "model": "gemini-flash",
    "notify_enabled": True,
    "webhook_url": "",
    "webhook_secret": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]


def make_random_png(size: int = 64) -> tuple[bytes, str]:
    """Create a small, dependency-free random PNG and return it with a sample id."""
    seed = secrets.randbits(64)
    colors = [secrets.token_bytes(3), secrets.token_bytes(3), secrets.token_bytes(3)]
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            color = colors[((x // 8) + (y // 8) + (seed & 3)) % len(colors)]
            row.extend(color)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
    return png, f"{seed:016x}"[:8]


class PatrolService:
    def __init__(self, account_pool, data_dir: str | Path = "data"):
        self.account_pool = account_pool
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "patrol_config.json"
        self.history_path = self.data_dir / "patrol_history.json"
        self.config = self._load_json(self.config_path, DEFAULT_CONFIG)
        self.history = self._load_json(self.history_path, [])
        if not isinstance(self.history, list):
            self.history = []
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
                "image_sample": None,
            }
            try:
                attachments = None
                prompt = TEXT_PROMPT
                if test_type == "image":
                    image, sample_id = make_random_png()
                    result["image_sample"] = sample_id
                    prompt = IMAGE_PROMPT
                    attachments = [{"data": image, "filename": f"patrol-{sample_id}.png", "mime": "image/png"}]
                response = await self.account_pool.generate(
                    prompt,
                    self.config.get("model", "gemini-flash"),
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
