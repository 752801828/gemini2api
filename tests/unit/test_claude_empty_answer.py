"""defect ⑩：空上游回答不能产出空文本块，也不能产出零长度 delta；两条流式分支还得
补上至少一个非空 delta，跟非流式侧的空块 padding 保持对称。

split_into_chunks("") 因 "".split(" ") == [""] 会吐出一个空字符串块；delta 循环若
没有真值判断，就会发出零长度的 content_block_delta。非流式侧同理：text="" 时旧代码
会拼出 content:[{"type":"text","text":""}]——一个空文本块，真实 API 不发；6cf8ca0 已经
把非流式侧改成发单空格占位文本块。但两条流式分支（真流式 _stream_claude 的 full_text、
buffered 流式 _stream_claude_buffered 的 text）当时被漏改：上游回答为空时只发
content_block_start(text:"") -> content_block_stop，中间零个 delta——同一个"遍历
content/delta，假设至少一块非空"的客户端假设一样会被打破。补上后，两条流式路径在空
上游回答时都必须发出恰好一个 text=" " 的 content_block_delta，不再是零个。"""
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


def test_empty_answer_real_stream_pads_single_nonempty_delta(gem_client, monkeypatch):
    """无 tools/attachments，走真流式路径。上游回答为空字符串时必须补发恰好一个非空
    （单空格）delta，不能是零个 delta（与非流式侧的空块 padding 保持对称）。"""
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
    assert len(deltas) == 1, f"空上游回答应恰好补一个 delta，实际: {deltas}"
    assert deltas[0]["delta"]["text"] == " "
    events = [ev for ev, _ in frames]
    assert events[0] == "message_start"
    assert "content_block_start" in events
    assert "content_block_stop" in events
    assert events[-1] == "message_stop"


def test_empty_answer_buffered_stream_pads_single_nonempty_delta(gem_client, monkeypatch):
    """有 tools（走 buffered 路径），上游文本为空、且不是合法 tool_calls JSON，落回纯文本
    分支。split_into_chunks("") 本身不吐真值块，但补的单空格占位要产出恰好一个非空 delta，
    不能是零个（与非流式侧的空块 padding 保持对称）。"""
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
    assert len(deltas) == 1, f"空上游回答应恰好补一个 delta，实际: {deltas}"
    assert deltas[0]["delta"]["text"] == " "
    events = [ev for ev, _ in frames]
    assert "content_block_start" in events
    assert "content_block_stop" in events
    assert events[-1] == "message_stop"
