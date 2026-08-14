import asyncio
from pathlib import Path
from app.core import stream as stream_mod

_ROOT = Path(__file__).resolve().parent.parent.parent
_AUTH = {"Authorization": "Bearer sk-test-key"}


def test_chat_realstream_emits_keepalive_ping_during_silence(gem_client, monkeypatch):
    """真流式路径：等上游的静默期必须发 : ping；答案与 [DONE] 照常（零回归）。"""
    import app.routers.openai as oai
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def slow_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        await asyncio.sleep(0.25)   # 静默 gap > interval → 多个 ping
        yield {"type": "delta", "text": "85"}
        yield {"type": "final", "text": "85", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(oai.gemini_client, "generate_stream", slow_generate_stream)
    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash", "messages": [{"role": "user", "content": "1+3+9*9"}], "stream": True,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert ": ping" in body        # 心跳出现
    assert "85" in body            # 答案照常
    assert "[DONE]" in body        # 正常收尾


def test_chat_realstream_no_ping_when_fast(gem_client, monkeypatch):
    """默认 10s 间隔 + 瞬时流 → 无 ping，行为与既有一致。"""
    import app.routers.openai as oai

    async def fast_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "delta", "text": "hi"}
        yield {"type": "final", "text": "hi", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(oai.gemini_client, "generate_stream", fast_generate_stream)
    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert ": ping" not in body
    assert "hi" in body and "[DONE]" in body


def test_openai_still_has_buffered_keepalive():
    """回归：buffered 路径心跳（既有）不受影响。"""
    src = (_ROOT / "app" / "routers" / "openai.py").read_text(encoding="utf-8")
    assert "_sse_keepalive_during(gen_task)" in src


def test_responses_stream_emits_keepalive_ping_during_silence(gem_client, monkeypatch):
    """/v1/responses 流式：等上游的静默期必须发 : ping；答案照常。"""
    import app.routers.responses as rp
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def slow_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        await asyncio.sleep(0.25)
        yield {"type": "delta", "text": "85"}
        yield {"type": "final", "text": "85", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(rp.gemini_client, "generate_stream", slow_generate_stream)
    with gem_client.stream("POST", "/v1/responses", json={
        "model": "gemini-pro", "input": "1+3+9*9", "stream": True,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert ": ping" in body
    assert "85" in body
