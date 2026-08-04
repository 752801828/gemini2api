import json
import asyncio

from app.core.patrol import PatrolService, make_random_png


class FakePool:
    def get_status(self):
        return {"accounts": [{"id": "a1", "label": "主账号"}]}

    async def generate(self, prompt, model, attachments=None, account_id=None):
        assert account_id == "a1"
        if attachments:
            assert attachments[0]["data"].startswith(b"\x89PNG\r\n\x1a\n")
        return {"text": "盘巡正常"}


def test_random_png_and_secret_masking(tmp_path):
    first, first_id = make_random_png()
    second, second_id = make_random_png()
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert first != second
    assert first_id != second_id

    service = PatrolService(FakePool(), tmp_path)
    public = service.update_config({
        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
        "webhook_secret": "secret-value",
    })
    assert public["webhook_configured"] is True
    assert public["secret_configured"] is True
    assert "webhook_secret" not in public
    assert json.loads((tmp_path / "patrol_config.json").read_text(encoding="utf-8"))["webhook_secret"] == "secret-value"


def test_round_records_text_and_random_image_results(tmp_path):
    service = PatrolService(FakePool(), tmp_path)
    asyncio.run(service._run_round("round-1", "manual"))

    overview = service.overview()
    assert overview["stats"]["history"] == {"rounds": 1, "tasks": 2, "success": 2}
    assert [task["type"] for task in overview["history"][0]["tasks"]] == ["text", "image"]
    assert overview["history"][0]["tasks"][1]["image_sample"]
