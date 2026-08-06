import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.patrol import IMAGE_PROMPTS, TEXT_PROMPTS, PatrolService
from app.routers.patrol import PatrolConfigUpdate

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def test_patrol_accepts_standard_and_extended_pro_flash_models():
    config = PatrolConfigUpdate(models=[
        "gemini-pro", "gemini-pro-thinking", "gemini-flash",
        "gemini-flash-thinking", "gemini-flash-lite",
    ])
    assert len(config.models) == 5


def test_patrol_accepts_image_ranges_above_five():
    config = PatrolConfigUpdate(image_min_count=10, image_max_count=20)
    assert (config.image_min_count, config.image_max_count) == (10, 20)


class FakePool:
    def __init__(self):
        self.calls = []

    def get_status(self):
        return {"accounts": [{"id": "a1", "label": "主账号"}]}

    async def generate(self, prompt, model, attachments=None, account_id=None):
        assert account_id == "a1"
        if attachments:
            assert attachments[0]["data"].startswith(b"\x89PNG\r\n\x1a\n")
        self.calls.append({"prompt": prompt, "model": model, "attachments": attachments})
        return {"text": "盘巡正常" * 100}


def test_image_library_and_secret_masking(tmp_path):
    service = PatrolService(FakePool(), tmp_path)
    image = service.add_image("sample.png", PNG)
    assert service.list_images()[0]["name"] == "sample.png"
    assert service.get_image(image["id"])[0].read_bytes() == PNG
    public = service.update_config({
        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
        "webhook_secret": "secret-value",
    })
    assert public["webhook_configured"] is True
    assert public["secret_configured"] is True
    assert "webhook_secret" not in public
    assert json.loads((tmp_path / "patrol_config.json").read_text(encoding="utf-8"))["webhook_secret"] == "secret-value"
    assert service.delete_image(image["id"]) is True
    assert service.list_images() == []


def test_round_records_text_and_random_image_results(tmp_path):
    pool = FakePool()
    service = PatrolService(pool, tmp_path)
    service.add_image("one.png", PNG)
    service.add_image("two.png", PNG + b"2")
    service.update_config({
        "models": ["gemini-flash", "gemini-pro"],
        "text_test_count": 2,
        "image_test_count": 3,
        "image_min_count": 1,
        "image_max_count": 2,
    })
    asyncio.run(service._run_round("round-1", "manual"))

    overview = service.overview()
    assert overview["stats"]["history"] == {"rounds": 1, "tasks": 5, "success": 5}
    assert [task["type"] for task in overview["history"][0]["tasks"]] == ["text", "text", "image", "image", "image"]
    assert [task["sequence"] for task in overview["history"][0]["tasks"]] == [1, 2, 1, 2, 3]
    assert overview["history"][0]["tasks"][0]["prompt"] in TEXT_PROMPTS
    assert overview["history"][0]["tasks"][2]["prompt"] in IMAGE_PROMPTS
    assert 1 <= len(overview["history"][0]["tasks"][2]["image_samples"]) <= 2
    assert len(overview["history"][0]["tasks"][2]["response"]) == 400
    assert len(overview["history"][0]["tasks"][2]["response_preview"]) == 300
    assert len(overview["history"][0]["tasks"][2]["image_sample_ids"]) == len(overview["history"][0]["tasks"][2]["image_samples"])
    assert all(call["model"] in {"gemini-flash", "gemini-pro"} for call in pool.calls)
    text_stats = overview["stats"]["types"]["text"]
    image_stats = overview["stats"]["types"]["image"]
    assert {key: text_stats[key] for key in ("tasks", "success", "failed", "rate")} == {"tasks": 2, "success": 2, "failed": 0, "rate": 100.0}
    assert {key: image_stats[key] for key in ("tasks", "success", "failed", "rate")} == {"tasks": 3, "success": 3, "failed": 0, "rate": 100.0}
    assert text_stats["avg_duration_ms"] >= 0
    assert image_stats["avg_duration_ms"] >= 0

    assert service.delete_round("round-1") is True
    updated = service.overview()
    assert updated["stats"]["history"] == {"rounds": 0, "tasks": 0, "success": 0}
    assert updated["stats"]["types"]["text"]["rate"] == 0.0
    assert service.delete_round("round-1") is False


def test_image_tasks_can_repeat_images_above_library_size(tmp_path):
    pool = FakePool()
    service = PatrolService(pool, tmp_path)
    service.add_image("only.png", PNG)
    service.update_config({
        "text_test_enabled": False,
        "image_test_enabled": True,
        "image_test_count": 1,
        "image_min_count": 10,
        "image_max_count": 20,
    })

    asyncio.run(service._run_round("round-repeat", "manual"))

    task = service.overview()["history"][0]["tasks"][0]
    assert 10 <= len(task["image_samples"]) <= 20
    assert set(task["image_samples"]) == {"only.png"}


def test_browser_failure_notification_reuses_patrol_webhook(tmp_path):
    service = PatrolService(FakePool(), tmp_path)
    service.config["webhook_url"] = "https://open.feishu.cn/open-apis/bot/v2/hook/example"
    service._send_feishu = AsyncMock(return_value={"sent": True, "error": ""})

    result = asyncio.run(service.notify_browser_failure(SimpleNamespace(id="a1", label="主账号"), "CK expired"))

    assert result["sent"] is True
    message, source = service._send_feishu.await_args.args
    assert "浏览器维护告警" in message
    assert "主账号（a1）" in message
    assert "CK expired" in message
    assert source == "browser maintenance"


def test_flow_browser_failure_notification_includes_email(tmp_path):
    service = PatrolService(FakePool(), tmp_path)
    service.config["webhook_url"] = "https://open.feishu.cn/open-apis/bot/v2/hook/example"
    service._send_feishu = AsyncMock(return_value={"sent": True, "error": ""})
    account = SimpleNamespace(
        id="flow-18",
        label="Brady Auclair",
        source="flow",
        flow_email="brady@example.com",
    )

    asyncio.run(service.notify_browser_failure(account, "browser unavailable"))

    message, source = service._send_feishu.await_args.args
    assert "Flow 邮箱：brady@example.com" in message
    assert source == "browser maintenance"


def test_patrol_type_stats_include_average_subtask_duration(tmp_path):
    service = PatrolService(FakePool(), tmp_path)
    service.history = [{
        "tasks": [
            {"type": "text", "success": True, "duration_ms": 100},
            {"type": "text", "success": False, "duration_ms": 300},
            {"type": "image", "success": True, "duration_ms": 500},
        ]
    }]

    stats = service.overview()["stats"]["types"]

    assert stats["text"]["avg_duration_ms"] == 200
    assert stats["image"]["avg_duration_ms"] == 500
