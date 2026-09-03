"""Codex CLI 恒带 tools → /v1/responses 流式必走 buffered 分支（has_tools 恒真）；
该路径此前 await generate 期间零字节，静默期超过网关空闲读超时会被杀连接（GAP ④）。"""
import asyncio
import contextlib

_AUTH = {"Authorization": "Bearer sk-test-key"}
_TOOLS = [{"type": "function", "name": "run_shell", "description": "run a shell cmd",
          "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]


def test_responses_buffered_emits_keepalive_before_first_payload(gem_client, monkeypatch):
    import app.routers.responses as rr
    from app.core import stream as stream_mod
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def slow_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        await asyncio.sleep(0.25)
        return {"text": "85", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(rr.gemini_client, "generate", slow_generate)
    with gem_client.stream("POST", "/v1/responses", json={
        "model": "gemini-pro", "input": "1+3+9*9", "stream": True, "tools": _TOOLS,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert ": ping" in body                            # 心跳出现
    assert "response.output_item.added" in body          # generate() 结束后的第一个真实 payload 帧
    # ping 必须先于第一个真实 payload 帧（created/in_progress 是结构性帧，不算 payload）
    assert body.index(": ping") < body.index("response.output_item.added")


def test_responses_buffered_error_path_unchanged(gem_client, monkeypatch):
    """generate 抛错时仍走原有 enc.failed() 帧，不被保活吞掉。"""
    import app.routers.responses as rr

    async def boom(prompt, model, conversation_id="", attachments=None, gem_id=None,
                   account_id=None, extended_thinking=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(rr.gemini_client, "generate", boom)
    with gem_client.stream("POST", "/v1/responses", json={
        "model": "gemini-pro", "input": "hi", "stream": True, "tools": _TOOLS,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert "boom" in body
    assert "response.failed" in body


def test_responses_buffered_cancels_gen_task_on_client_disconnect(monkeypatch):
    """客户端提前断开（生成器被 aclose，即 GeneratorExit）时，后台 generate task 必须被显式
    cancel，不能留成脱缰 task 继续跑完并占着账号槽位。驱动 _stream_gemini_response 这个独立的
    module-level 异步生成器：先拉 created/in_progress 两个结构性帧，再拉 ping 帧，然后 aclose()。
    用 asyncio.run，不用 pytest-asyncio（项目约定）。"""
    import app.routers.responses as rr
    from app.core import stream as stream_mod

    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)

    async def slow_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        await asyncio.sleep(5)   # long enough that aclose() always arrives first
        return {"text": "done", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(rr.gemini_client, "generate", slow_generate)

    async def run():
        tasks_before = asyncio.all_tasks()
        agen = rr._stream_gemini_response(
            None, "prompt", "gemini-pro", True, None, None, None, {}, False,
        )
        created = await agen.__anext__()
        assert "response.created" in created
        in_progress = await agen.__anext__()
        assert "response.in_progress" in in_progress
        ping = await agen.__anext__()          # pull the first keepalive frame
        assert ": ping" in ping

        new_tasks = asyncio.all_tasks() - tasks_before
        assert len(new_tasks) == 1              # exactly the gen_task created inside
        gen_task = next(iter(new_tasks))
        assert not gen_task.done()

        await agen.aclose()                      # simulate client disconnect
        with contextlib.suppress(asyncio.CancelledError):
            await gen_task
        assert gen_task.cancelled()               # slot-releasing cancellation actually landed

    asyncio.run(run())
