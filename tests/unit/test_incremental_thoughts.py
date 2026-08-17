from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent


def test_generate_stream_emits_incremental_thoughts_event():
    src = (_ROOT / "app" / "core" / "gemini_client.py").read_text(encoding="utf-8")
    # generate_stream 里有逐帧思考增量事件
    assert '{"type": "thoughts"' in src
    # 用独立累积游标做前缀 diff
    assert "emitted_thoughts" in src
    # 思考增量在答案 delta 之前 yield（guard 运行时 yield 语句本身，而非 docstring 提及顺序）
    assert src.index('yield {"type": "thoughts"') < src.index('yield {"type": "delta"'), \
        "思考增量必须在答案 delta 之前 yield"


_AUTH = {"Authorization": "Bearer sk-test-key"}


def test_openai_streams_reasoning_incrementally_before_answer(gem_client, monkeypatch):
    """思考事件应逐帧发 reasoning_content，且整段早于答案 content；final 不双发。"""
    import app.routers.openai as oai

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "thoughts", "text": "Analyzing "}
        yield {"type": "thoughts", "text": "the sky."}
        yield {"type": "delta", "text": "It is blue."}
        yield {"type": "final", "text": "It is blue.", "conversation_id": "",
               "images": [], "thoughts": "Analyzing the sky."}

    monkeypatch.setattr(oai.gemini_client, "generate_stream", fake_generate_stream)
    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash", "messages": [{"role": "user", "content": "why blue?"}],
        "reasoning_effort": "high", "stream": True,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert "Analyzing " in body and "the sky." in body       # reasoning streamed
    assert "It is blue." in body                              # answer present
    assert body.index("Analyzing ") < body.index("It is blue.")  # thinking BEFORE answer
    assert body.count("the sky.") == 1                        # not double-emitted at final


def test_openai_final_only_thoughts_still_emitted(gem_client, monkeypatch):
    """兜底回归：generate_stream 未发增量 thoughts（老式：只 delta+final 带 thoughts）时，
    final 仍把思考作为 reasoning_content 发出（前缀 diff 兜底）。"""
    import app.routers.openai as oai

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "delta", "text": "Ans"}
        yield {"type": "final", "text": "Ans", "conversation_id": "", "images": [],
               "thoughts": "late thoughts"}

    monkeypatch.setattr(oai.gemini_client, "generate_stream", fake_generate_stream)
    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "high", "stream": True,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert "late thoughts" in body and "Ans" in body and "[DONE]" in body


def test_openai_divergent_final_thoughts_not_reemitted(gem_client, monkeypatch):
    """final.thoughts 与已发 emitted_thoughts 不构成前缀延伸（发散/收缩）时，
    不得把整段 final thoughts 重新整段发出（避免与已流出的增量重复/混乱）。"""
    import app.routers.openai as oai

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "thoughts", "text": "ABCDE"}
        yield {"type": "delta", "text": "ans"}
        yield {"type": "final", "text": "ans", "conversation_id": "", "images": [],
               "thoughts": "XY"}

    monkeypatch.setattr(oai.gemini_client, "generate_stream", fake_generate_stream)
    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "high", "stream": True,
    }, headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert body.count("ABCDE") == 1        # incremental thoughts emitted exactly once, not re-sent
    assert "XY" not in body                 # divergent final thoughts suppressed, not emitted
