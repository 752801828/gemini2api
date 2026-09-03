"""defect ⑪：Anthropic 响应体的 null/字段噪音清理。

真实 API 按块类型只发相关字段（text 块无 id/name/input/source，tool_use 块无
text/source），但顶层 stop_sequence:null 恒发。之前 gemini2api 在每个 content
block 上都塞了一堆恒为 null 的字段，且 usage/stream 事件缺 cache_*/service_tier/
stop_sequence/citations，与真实 SDK v1.2.0 的响应体形状不一致。

R3：非流式 ClaudeResponse 的 text 块此前完全没有 citations 字段（流式的
content_block_start 一直都发 "citations": null），两侧不一致。现在 text 块的
citations 显式保留为 null（即使 v1.6.35 引入的 field_serializer 对 content 做了
exclude_none），tool_use 等其它块类型仍不带 citations；id/name/input/source 的
null 噪音清理（defect ⑪ 的成果）必须继续生效，不能被 R3 带回退。"""
import json

from app.models.claude import ClaudeResponse, ContentBlock, ClaudeUsage

_AUTH = {"Authorization": "Bearer sk-test-key"}


def _parse_sse(body: str):
    out = []
    for chunk in body.split("\n\n"):
        ev, data = None, None
        for line in chunk.split("\n"):
            if line.startswith(":") or not line.strip():
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if ev or data:
            out.append((ev, data))
    return out


# ---- 非流式：ClaudeResponse 序列化形状 ----

def test_text_response_content_block_has_no_null_noise():
    r = ClaudeResponse(id="msg_1", model="m", content=[ContentBlock(type="text", text="hi")])
    dumped = json.loads(r.model_dump_json())
    block = dumped["content"][0]
    # R3：text 块恒带 citations:null（真实 API 两侧都发）；id/name/input/source 的
    # null 噪音清理（defect ⑪）仍然生效，不能被 R3 带回退。
    assert block == {"type": "text", "text": "hi", "citations": None}
    assert "citations" in block
    # 顶层 stop_sequence:null 必须保留，不能被连带吞掉
    assert dumped["stop_sequence"] is None
    assert "stop_sequence" in dumped


def test_tool_use_response_content_block_has_no_null_noise():
    r = ClaudeResponse(
        id="msg_2", model="m", stop_reason="tool_use",
        content=[ContentBlock(type="tool_use", id="toolu_1", name="Bash", input={"command": "ls"})],
    )
    dumped = json.loads(r.model_dump_json())
    block = dumped["content"][0]
    assert block == {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    assert "text" not in block and "source" not in block
    # R3：tool_use 块不该带 citations（那是 text 块专属）——id/name/input/source 的
    # null 噪音清理（defect ⑪）继续生效。
    assert "citations" not in block
    assert dumped["stop_sequence"] is None


def test_usage_carries_cache_and_service_tier_fields():
    u = ClaudeUsage(input_tokens=5, output_tokens=7)
    dumped = json.loads(u.model_dump_json())
    assert dumped == {
        "input_tokens": 5, "output_tokens": 7,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "service_tier": "standard",
    }


def test_messages_endpoint_text_response_shape(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "hello there", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    # R3：非流式文本块恒带 citations:null（与流式的 content_block_start 对齐）。
    assert body["content"] == [{"type": "text", "text": "hello there", "citations": None}]
    assert body["stop_sequence"] is None
    assert body["usage"]["cache_creation_input_tokens"] == 0
    assert body["usage"]["cache_read_input_tokens"] == 0
    assert body["usage"]["service_tier"] == "standard"


def test_messages_endpoint_tool_use_response_shape(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {
            "text": '{"tool_calls":[{"name":"Bash","arguments":{"command":"ls"}}]}',
            "conversation_id": "", "images": [], "thoughts": "",
        }

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Bash", "description": "run cmd", "input_schema": {"type": "object"}}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["content"]) == 1
    block = body["content"][0]
    assert block["type"] == "tool_use"
    assert "text" not in block and "source" not in block
    # R3：tool_use 块不该带 citations（那是 text 块专属）。
    assert "citations" not in block
    assert body["stop_sequence"] is None


# ---- 流式：真流式路径（无 tools） ----

def test_real_stream_frames_carry_new_null_fields(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "delta", "text": "hello "}
        yield {"type": "delta", "text": "world"}
        yield {"type": "final", "text": "hello world", "conversation_id": "",
               "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate_stream", fake_generate_stream)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = dict(_parse_sse(body))
    msg_start = frames["message_start"]
    assert msg_start["message"]["stop_reason"] is None
    assert msg_start["message"]["stop_sequence"] is None
    assert msg_start["message"]["usage"]["cache_creation_input_tokens"] == 0
    assert msg_start["message"]["usage"]["cache_read_input_tokens"] == 0
    assert msg_start["message"]["usage"]["service_tier"] == "standard"

    cbs = frames["content_block_start"]
    assert cbs["content_block"]["citations"] is None

    msg_delta = frames["message_delta"]
    assert msg_delta["delta"]["stop_reason"] == "end_turn"
    assert msg_delta["delta"]["stop_sequence"] is None
    assert msg_delta["usage"]["cache_creation_input_tokens"] == 0
    assert msg_delta["usage"]["cache_read_input_tokens"] == 0
    assert msg_delta["usage"]["service_tier"] == "standard"


# ---- 流式：buffered 路径（有 tools） ----

def test_buffered_stream_text_frames_carry_new_null_fields(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "hello there", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = dict(_parse_sse(body))
    msg_start = frames["message_start"]
    assert msg_start["message"]["stop_reason"] is None
    assert msg_start["message"]["stop_sequence"] is None
    assert msg_start["message"]["usage"]["cache_creation_input_tokens"] == 0
    assert msg_start["message"]["usage"]["service_tier"] == "standard"

    cbs = frames["content_block_start"]
    assert cbs["content_block"]["citations"] is None

    msg_delta = frames["message_delta"]
    assert msg_delta["delta"]["stop_reason"] == "end_turn"
    assert msg_delta["delta"]["stop_sequence"] is None
    assert msg_delta["usage"]["cache_creation_input_tokens"] == 0
    assert msg_delta["usage"]["cache_read_input_tokens"] == 0
    assert msg_delta["usage"]["service_tier"] == "standard"


def test_buffered_stream_tool_use_message_delta_carries_new_fields(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {
            "text": '{"tool_calls":[{"name":"Bash","arguments":{"command":"ls"}}]}',
            "conversation_id": "", "images": [], "thoughts": "",
        }

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Bash", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = dict(_parse_sse(body))
    msg_delta = frames["message_delta"]
    assert msg_delta["delta"]["stop_reason"] == "tool_use"
    assert msg_delta["delta"]["stop_sequence"] is None
    assert msg_delta["usage"]["cache_creation_input_tokens"] == 0
    assert msg_delta["usage"]["cache_read_input_tokens"] == 0
    assert msg_delta["usage"]["service_tier"] == "standard"
