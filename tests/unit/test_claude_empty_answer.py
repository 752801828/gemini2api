"""defect ⑩：空上游回答不能产出空文本块，也不能产出零长度 delta。

split_into_chunks("") 因 "".split(" ") == [""] 会吐出一个空字符串块；delta 循环若
没有真值判断，就会发出零长度的 content_block_delta。非流式侧同理：text="" 时旧代码
会拼出 content:[{"type":"text","text":""}]——一个空文本块，真实 API 不发。"""
import json

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


def test_empty_answer_non_streaming_yields_single_nonempty_block(gem_client, monkeypatch):
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    # 恰好一块，不是 content:[]，也不是一个 text:"" 的空块
    assert len(body["content"]) == 1
    block = body["content"][0]
    assert block["type"] == "text"
    assert block["text"] != ""


def test_empty_answer_real_stream_emits_no_zero_length_delta(gem_client, monkeypatch):
    """无 tools/attachments，走真流式路径（该路径此前已有 if delta: 守卫，这里做回归钉住）。"""
    import app.routers.claude as cl

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "final", "text": "", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate_stream", fake_generate_stream)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = _parse_sse(body)
    deltas = [data for ev, data in frames if ev == "content_block_delta"]
    assert deltas == [], f"空上游回答不该产出任何 content_block_delta，实际: {deltas}"
    events = [ev for ev, _ in frames]
    assert events[0] == "message_start"
    assert "content_block_start" in events
    assert "content_block_stop" in events
    assert events[-1] == "message_stop"


def test_empty_answer_buffered_stream_emits_no_zero_length_delta(gem_client, monkeypatch):
    """有 tools（走 buffered 路径），上游文本为空、且不是合法 tool_calls JSON，
    落回纯文本分支，经 split_into_chunks("") 应仍然一个 delta 都不发。"""
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    frames = _parse_sse(body)
    deltas = [data for ev, data in frames if ev == "content_block_delta"]
    assert deltas == [], f"空上游回答不该产出任何 content_block_delta，实际: {deltas}"
    events = [ev for ev, _ in frames]
    assert "content_block_start" in events
    assert "content_block_stop" in events
    assert events[-1] == "message_stop"
