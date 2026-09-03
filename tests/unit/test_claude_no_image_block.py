"""defect ⑥：Anthropic assistant 侧 content-block union 没有 image 成员。

真实 SDK v1.2.0 验证过：严格校验对 type="image" 的 assistant 块报 35 个错误；宽松路径
会把它默默转成 text=None 的 TextBlock，isinstance(block, TextBlock) 的客户端直接崩，
按 type 过滤的客户端整块丢弃——两种情况下生成的图片都到不了用户手里。修复后改成
openai.py 既有做法：把生成图片渲染成 markdown 嵌进文字块，不单开 image 块。"""
import json

_AUTH = {"Authorization": "Bearer sk-test-key"}
_IMAGES = [{"id": "img_abc123", "mime": "image/png"}]


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


# ---- 非流式 ----

def test_non_streaming_image_answer_has_no_image_block(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "here is your image", "conversation_id": "", "images": _IMAGES, "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "draw me a cat"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(b["type"] != "image" for b in body["content"])
    assert len(body["content"]) == 1
    block = body["content"][0]
    assert block["type"] == "text"
    assert "/images/img_abc123" in block["text"]     # markdown 图片链接落进了文字块
    assert "here is your image" in block["text"]


def test_non_streaming_image_only_answer_still_one_text_block(gem_client, monkeypatch):
    """纯生图、上游没给文字描述：仍是恰好一个 text 块（图片 markdown 撑起来），不是空块。"""
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "", "conversation_id": "", "images": _IMAGES, "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "draw me a cat"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["content"]) == 1
    block = body["content"][0]
    assert block["type"] == "text"
    assert "/images/img_abc123" in block["text"]


# ---- buffered 流式（有 tools/attachments）----

def test_buffered_stream_image_answer_has_no_image_block(gem_client, monkeypatch):
    """prompt 特意不含生图意图字样——否则 create_message 会把 has_tools 强制置 False
    转去走真流式路径（见 is_image_generation_intent），这里要的是 buffered 路径本身。"""
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "here is your image", "conversation_id": "", "images": _IMAGES, "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "please help me with something"}],
        "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = _parse_sse(body)
    for ev, data in frames:
        if ev == "content_block_start":
            assert data["content_block"]["type"] != "image"
        assert "image" != (data.get("delta", {}) or {}).get("type")

    # 文字块（index 0）始终在 index 0，没被图片块挤走
    starts = [data for ev, data in frames if ev == "content_block_start"]
    assert len(starts) == 1
    assert starts[0]["index"] == 0
    assert starts[0]["content_block"]["type"] == "text"

    deltas = "".join(
        data["delta"]["text"] for ev, data in frames
        if ev == "content_block_delta" and data["delta"]["type"] == "text_delta"
    )
    assert "/images/img_abc123" in deltas
    assert "here is your image" in deltas


# ---- 真流式（无 tools/attachments）----

def test_real_stream_image_answer_has_no_image_block(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "delta", "text": "here "}
        yield {"type": "delta", "text": "is your image"}
        yield {"type": "final", "text": "here is your image", "conversation_id": "",
               "images": _IMAGES, "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate_stream", fake_generate_stream)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "draw me a cat"}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = _parse_sse(body)
    starts = [data for ev, data in frames if ev == "content_block_start"]
    assert len(starts) == 1
    assert starts[0]["index"] == 0
    assert starts[0]["content_block"]["type"] == "text"      # 没被图片块挤出 index 0
    for ev, data in frames:
        assert "image" != (data.get("delta", {}) or {}).get("type")

    deltas = "".join(
        data["delta"]["text"] for ev, data in frames
        if ev == "content_block_delta" and data["delta"]["type"] == "text_delta"
    )
    assert "/images/img_abc123" in deltas       # 图片 markdown 作为追加 delta 补进了同一文本块
    assert "here is your image" in deltas
