"""Claude Code 恒带 tools → 必走 _stream_claude_buffered；该路径此前 await generate 期间零字节。"""
import asyncio

_AUTH = {"Authorization": "Bearer sk-test-key"}


def test_claude_buffered_emits_keepalive_during_slow_generate(gem_client, monkeypatch):
    import app.routers.claude as cl
    from app.core import stream as stream_mod
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def slow_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        await asyncio.sleep(0.25)
        return {"text": "done", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", slow_generate)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert ": ping" in body                       # 静默期有心跳
    assert "message_start" in body                # 协议事件仍在
    assert "message_stop" in body
    assert body.index(": ping") < body.index("message_start")  # 心跳先于首个事件（此前零字节）


def test_claude_buffered_error_path_unchanged(gem_client, monkeypatch):
    """generate 抛错时仍走原有 error 事件，不被保活吞掉。"""
    import app.routers.claude as cl

    async def boom(prompt, model, conversation_id="", attachments=None, gem_id=None,
                   account_id=None, extended_thinking=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(cl.gemini_client, "generate", boom)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert "boom" in body and "error" in body


def test_openai_keepalive_alias_still_exported():
    """回归：openai.py 仍暴露 _sse_keepalive_during（既有源码守卫测试依赖它）。"""
    import app.routers.openai as oai
    assert callable(oai._sse_keepalive_during)


def test_claude_buffered_cancels_gen_task_on_client_disconnect(monkeypatch):
    """FIX A 钉住：客户端提前断开（生成器被 aclose，即 GeneratorExit）时，后台 generate task
    必须被显式 cancel，不能留成脱缰 task 继续跑完并占着账号槽位。

    driving a true HTTP-level disconnect through TestClient's synchronous streaming iterator
    isn't reliably deterministic here (would need to abort a background thread mid-iteration),
    so per the fallback specified: directly exercise the async generator — pull exactly one
    frame (the keepalive ping) via __anext__(), then aclose() it to simulate disconnect, and
    assert the task ends up cancelled. Uses asyncio.run per project convention (no
    pytest-asyncio / no @pytest.mark.asyncio)."""
    import contextlib
    import app.routers.claude as cl
    from app.core import stream as stream_mod

    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def slow_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        await asyncio.sleep(5)   # long enough that aclose() always arrives first
        return {"text": "done", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", slow_generate)

    async def run():
        tasks_before = asyncio.all_tasks()
        agen = cl._stream_claude_buffered("prompt", "gemini-pro", False, [], "msg_1")
        first = await agen.__anext__()          # pull exactly the first (keepalive) frame
        assert ": ping" in first
        new_tasks = asyncio.all_tasks() - tasks_before
        assert len(new_tasks) == 1               # exactly the gen_task created inside
        gen_task = next(iter(new_tasks))
        assert not gen_task.done()

        await agen.aclose()                      # simulate client disconnect
        with contextlib.suppress(asyncio.CancelledError):
            await gen_task
        assert gen_task.cancelled()               # slot-releasing cancellation actually landed

    asyncio.run(run())


def _parse_sse(body: str):
    """把 SSE 文本解析成 [(event, data_json)]，忽略注释行(: ping)。"""
    import json as _json
    out = []
    for chunk in body.split("\n\n"):
        ev, data = None, None
        for line in chunk.split("\n"):
            if line.startswith(":") or not line.strip():
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = _json.loads(line[5:].strip())
        if ev or data:
            out.append((ev, data))
    return out


def test_claude_stream_frames_are_parseable_with_event_field(gem_client, monkeypatch):
    """官方 Anthropic SDK 按 event: 字段分发；纯 data: 帧会被解析成零事件。
    钉住 buffered 路径（Claude Code 恒带 tools）：每帧必须带 event:，且 event == data["type"]。
    同时用短 keepalive 间隔 + 慢 generate 真正压出 ping 注释帧，证明它们绝不会被解析成事件。"""
    import app.routers.claude as cl
    from app.core import stream as stream_mod
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        await asyncio.sleep(0.25)  # 静默期够长，确保真的压出 ": ping\n\n" 注释帧
        return {"text": "done", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    with gem_client.stream("POST", "/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read", "description": "d", "input_schema": {"type": "object"}}],
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())

    # pings were genuinely emitted during the slow generate() window
    assert body.count(": ping\n\n") >= 1

    frames = _parse_sse(body)
    assert frames, "no SSE frames parsed"
    for ev, data in frames:
        assert ev is not None, f"frame missing event: field: {data}"
        assert data is not None
        assert ev == data["type"], f"event {ev!r} != data.type {data['type']!r}"
    assert frames[0][0] == "message_start"
    assert frames[-1][0] == "message_stop"
    # comment frames (": ping") must never surface as a parsed (event, data) pair
    assert all(ev != ": ping" for ev, _ in frames)


def test_claude_real_stream_frames_are_parseable_with_event_field(gem_client, monkeypatch):
    """同一断言，覆盖非 buffered 的真流式路径（无 tools，走 _stream_claude 的真流式分支）。"""
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

    frames = _parse_sse(body)
    assert frames, "no SSE frames parsed"
    for ev, data in frames:
        assert ev is not None, f"frame missing event: field: {data}"
        assert data is not None
        assert ev == data["type"], f"event {ev!r} != data.type {data['type']!r}"
    assert frames[0][0] == "message_start"
    assert frames[-1][0] == "message_stop"
