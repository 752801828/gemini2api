import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.patrol import IMAGE_PROMPTS, TEXT_PROMPTS, PatrolService

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


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
    assert overview["stats"]["types"]["text"] == {"tasks": 2, "success": 2, "failed": 0, "rate": 100.0}
    assert overview["stats"]["types"]["image"] == {"tasks": 3, "success": 3, "failed": 0, "rate": 100.0}

    assert service.delete_round("round-1") is True
    updated = service.overview()
    assert updated["stats"]["history"] == {"rounds": 0, "tasks": 0, "success": 0}
    assert updated["stats"]["types"]["text"]["rate"] == 0.0
    assert service.delete_round("round-1") is False


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
