"""issue #10 后续：Claude Code 的 tool_use / tool_result 块此前被整段丢弃，agent 循环从第二轮起为空。"""
from app.utils.prompt import build_prompt_from_messages

_ROUNDTRIP = [
    {"role": "user", "content": [{"type": "text", "text": "read foo.py"}]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "I'll read it."},
        {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "foo.py"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": [{"type": "text", "text": "print('hello world')"}]}]},
]


def test_tool_roundtrip_survives_into_prompt():
    p = build_prompt_from_messages(_ROUNDTRIP, system="You are Claude Code.")
    assert "hello world" in p          # tool_result 内容不再丢失
    assert "Read" in p                 # tool_use 的工具名在
    assert "foo.py" in p               # tool_use 的入参在
    assert not p.rstrip().endswith("Human:")   # 结尾不再是空的 Human:


def test_tool_result_string_content():
    p = build_prompt_from_messages(
        [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "RESULT_TEXT"}]}])
    assert "RESULT_TEXT" in p


def test_tool_use_without_input():
    p = build_prompt_from_messages(
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Ping"}]}])
    assert "Ping" in p


def test_non_str_text_does_not_crash():
    """既存缺陷：块内 text 非字符串会在 join 处抛 TypeError→500。现应优雅跳过。"""
    for bad in (None, 123, {"a": 1}, ["x"]):
        p = build_prompt_from_messages([{"role": "user", "content": [{"type": "text", "text": bad}]}])
        assert isinstance(p, str)


def test_plain_text_blocks_regression():
    """零回归：既有 text 块行为逐字不变（四协议共用此函数）。"""
    p = build_prompt_from_messages(
        [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
         {"role": "assistant", "content": "plain"}], system="S")
    assert p == "System: S\n\nHuman: a\nb\n\nAssistant: plain"


def test_unknown_blocks_still_skipped():
    p = build_prompt_from_messages(
        [{"role": "user", "content": [{"type": "image", "source": {"data": "x"}}, {"type": "text", "text": "hi"}]}])
    assert p == "Human: hi"


_AUTH = {"Authorization": "Bearer sk-test-key"}


def test_openai_conversation_continuation_non_str_text_no_500(gem_client, monkeypatch):
    """openai.py 的会话续接分支（gemini_conv_id 命中，只发最新一条用户消息）同款 flatten
    缺陷：block.text 非字符串此前在 "\\n".join 处抛 TypeError -> 500，现应优雅跳过。"""
    import app.routers.openai as oai
    from app.core.conversation_store import Conversation

    fake_conv = Conversation("conv-1", gemini_conv_id="gconv-1")

    async def fake_get(conv_id):
        return fake_conv

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        return {"text": "ok", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(oai.conversation_store, "get", fake_get)
    monkeypatch.setattr(oai.gemini_client, "generate", fake_generate)

    r = gem_client.post("/v1/chat/completions", json={
        "model": "gemini-flash",
        "conversation_id": "conv-1",
        "messages": [{"role": "user", "content": [{"type": "text", "text": None}]}],
    }, headers=_AUTH)
    assert r.status_code == 200
