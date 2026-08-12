import asyncio
import json

from app.core.log_store import (
    LogStore,
    capture_response_iterator,
    create_log_record,
    decode_logged_response,
    limit_log_payload,
)


def test_log_payload_keeps_messages_but_removes_secrets_and_images():
    payload = limit_log_payload({
        "model": "gemini-flash",
        "api_key": "secret-value",
        "x-api-key": "another-secret",
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAAAA"}},
                {"type": "input_image", "b64_json": "AAAAAA"},
            ],
        }],
    })

    assert payload["api_key"] == "****"
    assert payload["x-api-key"] == "****"
    assert payload["max_tokens"] == 1024
    assert payload["messages"][0]["content"][0]["text"] == "describe this"
    assert "base64 omitted" in payload["messages"][0]["content"][1]["image_url"]["url"]
    assert "base64 omitted" in payload["messages"][0]["content"][2]["b64_json"]


def test_json_response_is_saved_on_same_log_record(tmp_path):
    store = LogStore(persist_path=str(tmp_path / "logs.json"))
    record = create_log_record(
        method="POST",
        path="/v1/chat/completions",
        model="gemini-flash",
        request_body={"messages": [{"role": "user", "content": "hello"}]},
    )
    store.add(record)
    response = {"choices": [{"message": {"content": "world"}}]}

    async def body():
        yield json.dumps(response).encode()

    async def consume():
        chunks = []
        async for chunk in capture_response_iterator(
            body(), store, record.id, "application/json", False
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    delivered = asyncio.run(consume())
    saved = store.get(record.id)

    assert json.loads(delivered) == response
    assert saved["request"]["messages"][0]["content"] == "hello"
    assert saved["response"] == response


def test_stream_response_records_bounded_preview():
    result = decode_logged_response(
        b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
        "text/event-stream", True, False,
    )

    assert result["stream"] is True
    assert "hello" in result["preview"]
