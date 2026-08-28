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
